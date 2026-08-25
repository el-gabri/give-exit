"""Tests for chunking, embeddings, vector stores and retrieval."""

import hashlib
import sys
from types import SimpleNamespace

import pytest

from app.rag.chunking import SectionAwareChunker, is_heading
from app.rag.embeddings import MockEmbeddingClient
from app.rag.pipeline import RagPipeline, RetrievalBatchError
from app.rag.vector_store import ChromaVectorStore, InMemoryVectorStore, _cosine
from app.schemas.document import DocumentPage, ExtractionMethod, ParsedDocument

FACTS = (
    "A autora contratou os servicos do banco reu em janeiro de 2024. "
    "Apos o contrato, verificou cobrancas indevidas em sua fatura mensal, "
    "totalizando prejuizo material significativo ao longo de seis meses."
)
LAW = (
    "Aplica-se o Codigo de Defesa do Consumidor, artigo 42, paragrafo unico, "
    "que garante a repeticao do indebito em dobro nas cobrancas indevidas."
)
REQUESTS = (
    "Requer a condenacao do reu ao pagamento de indenizacao por danos morais "
    "no valor de R$ 20.000,00 e a restituicao em dobro dos valores cobrados."
)


def _petition() -> ParsedDocument:
    return ParsedDocument(
        filename="peticao.pdf",
        pages=[
            DocumentPage(number=1, text=f"DOS FATOS\n\n{FACTS}"),
            DocumentPage(number=2, text=f"DO DIREITO\n\n{LAW}"),
            DocumentPage(number=3, text=f"DOS PEDIDOS\n\n{REQUESTS}"),
        ],
        language="pt",
        extraction_method=ExtractionMethod.NATIVE_TEXT,
    )


def test_chroma_adapter_is_embedded_and_disables_collection_embedding_functions(
    monkeypatch: pytest.MonkeyPatch, tmp_path
) -> None:
    calls: dict[str, object] = {}
    collection = object()

    class FakeClient:
        def get_or_create_collection(self, **kwargs):
            calls.update(kwargs)
            return collection

    def persistent_client(*, path: str) -> FakeClient:
        calls["path"] = path
        return FakeClient()

    # Deliberately expose no HttpClient: constructing the adapter would fail
    # if a future refactor silently changed the accepted security boundary.
    monkeypatch.setitem(
        sys.modules,
        "chromadb",
        SimpleNamespace(PersistentClient=persistent_client),
    )

    store = ChromaVectorStore(tmp_path, collection_name="embedded-only")

    assert store.index_name == "embedded-only"
    assert calls["path"] == str(tmp_path)
    assert calls["embedding_function"] is None


def test_heading_detection() -> None:
    assert is_heading("DOS FATOS")
    assert is_heading("II - DO DIREITO")
    assert not is_heading("A autora contratou os servicos do banco reu.")
    assert not is_heading("")
    assert not is_heading("R$ 50.000,00")  # too few letters to qualify


def test_chunker_respects_sections_and_provenance() -> None:
    chunks = SectionAwareChunker(target_chars=1200, overlap_chars=100).chunk(_petition())

    sections = {c.section for c in chunks}
    assert sections == {"DOS FATOS", "DO DIREITO", "DOS PEDIDOS"}
    # no chunk mixes sections; provenance points at the right page
    pedidos = next(c for c in chunks if c.section == "DOS PEDIDOS")
    assert pedidos.page_start == 3
    assert "danos morais" in pedidos.text
    # ids are stable and content-scoped
    assert all(c.chunk_id.startswith(c.doc_id) for c in chunks)


def test_chunker_splits_oversized_sections_with_overlap() -> None:
    long_text = " ".join(f"paragrafo numero {i} do documento juridico." for i in range(200))
    doc = ParsedDocument(
        filename="x.pdf",
        pages=[DocumentPage(number=1, text=f"DOS FATOS\n\n{long_text}")],
        language="pt",
        extraction_method=ExtractionMethod.NATIVE_TEXT,
    )
    chunker = SectionAwareChunker(target_chars=500, overlap_chars=80)
    chunks = chunker.chunk(doc)

    assert len(chunks) > 1
    assert all(len(c.text) <= 500 + 100 for c in chunks)  # target + heading prefix slack


def test_chunker_rejects_invalid_overlap() -> None:
    with pytest.raises(ValueError):
        SectionAwareChunker(target_chars=100, overlap_chars=100)


async def test_mock_embeddings_are_deterministic_and_semanticish() -> None:
    embedder = MockEmbeddingClient()
    [a1] = await embedder.embed(["cobranca indevida danos morais"])
    [a2] = await embedder.embed(["cobranca indevida danos morais"])
    [related] = await embedder.embed(["indenizacao por danos morais"])
    [unrelated] = await embedder.embed(["contrato de trabalho ferias salario"])

    assert a1 == a2  # deterministic
    assert _cosine(a1, related) > _cosine(a1, unrelated)  # shared vocab ranks higher


async def test_pipeline_indexes_and_retrieves_relevant_section() -> None:
    pipeline = RagPipeline(
        embedder=MockEmbeddingClient(),
        store=InMemoryVectorStore(),
        default_k=2,
        include_trace_previews=True,
    )
    doc = _petition()
    chunks = await pipeline.index_document(doc)
    assert len(chunks) == 3

    results = await pipeline.retrieve(
        "indenizacao por danos morais valor pedido", doc_id=doc.doc_id
    )
    assert results
    assert results[0].chunk.section == "DOS PEDIDOS"


async def test_pipeline_supports_legacy_embedder_without_model_name() -> None:
    class LegacyEmbedder:
        def __init__(self) -> None:
            self._delegate = MockEmbeddingClient()

        async def embed(self, texts: list[str]) -> list[list[float]]:
            return await self._delegate.embed(texts)

    pipeline = RagPipeline(
        embedder=LegacyEmbedder(), store=InMemoryVectorStore(), default_k=1
    )
    doc = _petition()
    await pipeline.index_document(doc)

    results, trace = await pipeline.retrieve_with_trace(
        "danos morais", doc_id=doc.doc_id, agent="legacy"
    )

    assert results
    assert trace.embedding_model == "LegacyEmbedder"


async def test_reindex_removes_chunks_left_by_an_older_chunking_layout() -> None:
    store = InMemoryVectorStore()
    embedder = MockEmbeddingClient()
    long_facts = " ".join(
        f"paragrafo juridico {index} com fatos e provas" for index in range(80)
    )
    doc = ParsedDocument(
        filename="reindex.pdf",
        pages=[DocumentPage(number=1, text=f"DOS FATOS\n\n{long_facts}")],
        language="pt",
        extraction_method=ExtractionMethod.NATIVE_TEXT,
    )
    many_chunk_pipeline = RagPipeline(
        embedder=embedder,
        store=store,
        chunker=SectionAwareChunker(target_chars=200, overlap_chars=20),
        default_k=100,
    )
    old_chunks = await many_chunk_pipeline.index_document(doc)
    assert len(old_chunks) > 1

    few_chunk_pipeline = RagPipeline(
        embedder=embedder,
        store=store,
        chunker=SectionAwareChunker(target_chars=5000, overlap_chars=20),
        default_k=100,
    )
    current_chunks = await few_chunk_pipeline.index_document(doc)
    results = await few_chunk_pipeline.retrieve("fatos provas", doc_id=doc.doc_id)

    assert len(current_chunks) == 1
    assert [item.chunk.chunk_id for item in results] == [current_chunks[0].chunk_id]


async def test_retrieval_trace_preserves_rank_score_and_source_provenance() -> None:
    pipeline = RagPipeline(
        embedder=MockEmbeddingClient(),
        store=InMemoryVectorStore(),
        default_k=2,
        include_trace_previews=True,
    )
    doc = _petition()
    await pipeline.index_document(doc)

    query = "indenizacao por danos morais"
    results, trace = await pipeline.retrieve_with_trace(
        query,
        doc_id=doc.doc_id,
        agent="legal_analysis",
    )

    assert trace.agent == "legal_analysis"
    assert trace.doc_id == doc.doc_id
    assert trace.query == query
    assert trace.query_sha256 == hashlib.sha256(query.encode()).hexdigest()
    assert trace.requested_k == 2
    assert trace.returned_count == len(results) == 2
    assert trace.embedding_model.startswith("mock-hashed-bow-v1")
    assert trace.vector_store == "InMemoryVectorStore"
    assert "section-aware-v1" in trace.index_version
    assert trace.batch_id
    assert trace.batch_duration_ms >= 0
    assert [item.rank for item in trace.results] == [1, 2]
    for result, audited in zip(results, trace.results, strict=True):
        assert audited.chunk_id == result.chunk.chunk_id
        assert audited.page_start == result.chunk.page_start
        assert audited.page_end == result.chunk.page_end
        assert audited.score == result.score
        assert audited.content_sha256 == hashlib.sha256(
            result.chunk.text.encode()
        ).hexdigest()
        assert audited.text_preview is not None
        assert len(audited.text_preview) <= 240


async def test_retrieval_trace_omits_text_previews_by_default() -> None:
    pipeline = RagPipeline(
        embedder=MockEmbeddingClient(), store=InMemoryVectorStore()
    )
    doc = _petition()
    await pipeline.index_document(doc)

    _, trace = await pipeline.retrieve_with_trace(
        "danos morais", doc_id=doc.doc_id, agent="legal_analysis"
    )

    assert trace.results
    assert all(item.text_preview is None for item in trace.results)


async def test_retrieval_rejects_non_positive_k() -> None:
    pipeline = RagPipeline(
        embedder=MockEmbeddingClient(), store=InMemoryVectorStore()
    )
    doc = _petition()
    await pipeline.index_document(doc)

    with pytest.raises(ValueError, match="k must be positive"):
        await pipeline.retrieve("danos morais", doc_id=doc.doc_id, k=0)


async def test_retrieve_many_batches_query_embeddings() -> None:
    class RecordingEmbedder(MockEmbeddingClient):
        def __init__(self) -> None:
            super().__init__()
            self.calls: list[list[str]] = []

        async def embed(self, texts: list[str]) -> list[list[float]]:
            self.calls.append(texts)
            return await super().embed(texts)

    embedder = RecordingEmbedder()
    pipeline = RagPipeline(embedder=embedder, store=InMemoryVectorStore(), default_k=2)
    doc = _petition()
    await pipeline.index_document(doc)
    embedder.calls.clear()

    results = await pipeline.retrieve_many(
        ["danos morais", "restituicao em dobro"], doc_id=doc.doc_id
    )

    assert len(results) == 2
    assert embedder.calls == [["danos morais", "restituicao em dobro"]]


async def test_retrieval_batch_preserves_siblings_when_one_lookup_fails() -> None:
    class OneLookupFailsStore(InMemoryVectorStore):
        def __init__(self) -> None:
            super().__init__()
            self.query_calls = 0

        async def query(self, vector, doc_id, k):  # type: ignore[override]
            self.query_calls += 1
            if self.query_calls == 2:
                raise RuntimeError("vector store unavailable")
            return await super().query(vector, doc_id, k)

    store = OneLookupFailsStore()
    pipeline = RagPipeline(
        embedder=MockEmbeddingClient(), store=store, default_k=2
    )
    doc = _petition()
    await pipeline.index_document(doc)
    queries = ["fatos", "direito", "pedidos"]

    with pytest.raises(RetrievalBatchError) as caught:
        await pipeline.retrieve_many_with_traces(
            queries, doc_id=doc.doc_id, agent="legal_analysis"
        )

    traces = caught.value.traces
    assert store.query_calls == len(queries)
    assert [trace.query for trace in traces] == queries
    assert [trace.query_index for trace in traces] == [0, 1, 2]
    assert len({trace.batch_id for trace in traces}) == 1
    assert traces[1].error == "RuntimeError: vector store unavailable"
    assert traces[1].results == []
    assert traces[1].returned_count == 0
    assert traces[0].error is None and traces[0].results
    assert traces[2].error is None and traces[2].results


async def test_embedding_failure_creates_a_trace_for_every_query() -> None:
    class BrokenEmbedder(MockEmbeddingClient):
        async def embed(self, texts: list[str]) -> list[list[float]]:
            raise RuntimeError("embedding provider unavailable")

    pipeline = RagPipeline(
        embedder=BrokenEmbedder(), store=InMemoryVectorStore(), default_k=2
    )
    queries = ["fatos", "pedidos"]

    with pytest.raises(RetrievalBatchError) as caught:
        await pipeline.retrieve_many_with_traces(
            queries, doc_id="doc-a", agent="legal_analysis"
        )

    traces = caught.value.traces
    assert [trace.query for trace in traces] == queries
    assert len({trace.batch_id for trace in traces}) == 1
    assert all(trace.returned_count == 0 for trace in traces)
    assert all(trace.results == [] for trace in traces)
    assert all(
        trace.error == "RuntimeError: embedding provider unavailable"
        for trace in traces
    )


async def test_embedding_cardinality_mismatch_fails_with_complete_audit() -> None:
    class ShortEmbedder(MockEmbeddingClient):
        async def embed(self, texts: list[str]) -> list[list[float]]:
            return await super().embed(texts[:-1])

    pipeline = RagPipeline(
        embedder=ShortEmbedder(), store=InMemoryVectorStore(), default_k=2
    )
    queries = ["fatos", "pedidos"]

    with pytest.raises(RetrievalBatchError) as caught:
        await pipeline.retrieve_many_with_traces(
            queries, doc_id="doc-a", agent="legal_analysis"
        )

    traces = caught.value.traces
    assert [trace.query for trace in traces] == queries
    assert all(trace.returned_count == 0 for trace in traces)
    assert all(
        trace.error == "ValueError: embedding provider returned 1 vectors for 2 queries"
        for trace in traces
    )


async def test_retrieval_is_isolated_per_document() -> None:
    pipeline = RagPipeline(
        embedder=MockEmbeddingClient(), store=InMemoryVectorStore(), default_k=5
    )
    doc = _petition()
    await pipeline.index_document(doc)

    results = await pipeline.retrieve("danos morais", doc_id="other-doc-id")
    assert results == []  # never leak chunks across documents


async def test_pipeline_can_delete_an_indexed_document() -> None:
    pipeline = RagPipeline(
        embedder=MockEmbeddingClient(), store=InMemoryVectorStore(), default_k=5
    )
    doc = _petition()
    await pipeline.index_document(doc)

    await pipeline.delete_document(doc.doc_id)

    assert await pipeline.retrieve("danos morais", doc_id=doc.doc_id) == []


async def test_chroma_adapter_roundtrip(tmp_path) -> None:
    chromadb = pytest.importorskip("chromadb")  # noqa: F841
    from app.rag.vector_store import ChromaVectorStore

    pipeline = RagPipeline(
        embedder=MockEmbeddingClient(),
        store=ChromaVectorStore(persist_dir=tmp_path / "chroma"),
        default_k=2,
    )
    doc = _petition()
    await pipeline.index_document(doc)

    results = await pipeline.retrieve("restituicao em dobro danos morais", doc_id=doc.doc_id)
    assert results
    assert results[0].chunk.doc_id == doc.doc_id
    assert results[0].chunk.page_start >= 1
