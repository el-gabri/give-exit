"""Evidence citations must name the file the quoted words actually came from.

A notice that attributes one document's text to another document is worse than
a shorter notice: it is a false statement of proof in a legal artifact, and the
per-citation hash makes the wrong attribution look verified.
"""

import hashlib
from pathlib import Path

import fitz
import httpx
import pytest

from app.api.main import create_app
from app.consumer.service import _clean_chunk_quote, _markdown_inline
from app.core.config import LLMProvider, Settings, VectorStoreBackend
from app.rag.chunking import is_heading
from app.reporting.convert import strip_markdown_escapes
from app.schemas.rag import RetrievedChunk
from app.schemas.trace import RetrievalTrace, RetrievedItemTrace

FATURA = (
    "FATURA DA LOJA EXEMPLO. Cobranca nao reconhecida de R$ 250,00 lancada em "
    "10/03/2026 na fatura do cartao. Protocolo ABC-123 aberto no atendimento."
)
CONTRATO = (
    "CONTRATO DA OPERADORA XPTO. O cliente autorizou expressamente o debito "
    "automatico mensal e declarou ciencia integral das clausulas contratuais."
)


def _evidence_traces(
    result_sets: list[list[RetrievedChunk]],
) -> list[RetrievalTrace]:
    traces: list[RetrievalTrace] = []
    for query_index, results in enumerate(result_sets):
        query = f"evidence query {query_index}"
        traces.append(
            RetrievalTrace(
                batch_id="evidence-batch",
                agent="consumer_case_evidence",
                doc_id=results[0].chunk.doc_id if results else "evidence-doc",
                query_index=query_index,
                query=query,
                query_sha256=hashlib.sha256(query.encode()).hexdigest(),
                requested_k=max(1, len(results)),
                candidate_k=max(1, len(results)),
                returned_count=len(results),
                retrieval_mode="hybrid",
                embedding_model="test",
                vector_store="memory",
                index_version="test",
                chunking_version="test",
                score_type="rrf_score",
                rrf_constant=60,
                dense_weight=1.0,
                lexical_weight=1.0,
                results=[
                    RetrievedItemTrace(
                        rank=rank,
                        chunk_id=result.chunk.chunk_id,
                        doc_id=result.chunk.doc_id,
                        page_start=result.chunk.page_start,
                        page_end=result.chunk.page_end,
                        score=result.score,
                        content_sha256=hashlib.sha256(
                            result.chunk.text.encode()
                        ).hexdigest(),
                    )
                    for rank, result in enumerate(results, start=1)
                ],
            )
        )
    return traces


def _pdf_bytes(text: str) -> bytes:
    document = fitz.open()
    page = document.new_page()
    page.insert_textbox(
        fitz.Rect(50, 50, page.rect.width - 50, page.rect.height - 50), text, fontsize=9
    )
    payload = document.tobytes()
    document.close()
    return payload


class _FakeOcr:
    def ocr_image(self, image_bytes: bytes) -> str:
        return "COMPROVANTE DE COMPRA com protocolo ABC-123 e valor de R$ 100,00."


@pytest.fixture
async def notice_client(tmp_path: Path, monkeypatch):
    monkeypatch.setattr("app.api.main.create_default_ocr_engine", lambda: _FakeOcr())
    settings = Settings(
        llm_provider=LLMProvider.MOCK,
        vector_store=VectorStoreBackend.MEMORY,
        data_dir=tmp_path / "data",
        _env_file=None,
    )
    app = create_app(settings)
    async with app.router.lifespan_context(app):
        await app.state.consumer_service.prepare_legal_corpus()
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(
            transport=transport, base_url="http://test", timeout=120
        ) as client:
            yield client


async def _case_with_documents(
    client: httpx.AsyncClient, documents: list[tuple[str, str]]
) -> tuple[str, dict[str, str]]:
    created = (await client.post("/consumer/cases")).json()
    case_id, headers = created["case_id"], {"X-Consumer-Case-Token": created["case_token"]}
    for filename, body in documents:
        uploaded = await client.post(
            f"/consumer/cases/{case_id}/documents",
            headers=headers,
            files={"file": (filename, _pdf_bytes(body), "application/pdf")},
        )
        assert uploaded.status_code == 201, uploaded.text
    confirmed = await client.patch(
        f"/consumer/cases/{case_id}/facts",
        headers=headers,
        json={
            "consumer_name": "Maria Souza",
            "bank_name": "Loja Exemplo",
            "issue_category": "unauthorized_charge",
            "complaint_summary": "A Loja Exemplo cobrou R$ 250,00 na fatura sem autorizacao.",
            "incident_date_or_period": "10/03/2026",
            "desired_resolution": "Quero o estorno integral da cobranca indevida.",
            "facts_confirmed": True,
        },
    )
    assert confirmed.status_code == 200, confirmed.text
    return case_id, headers


async def test_each_citation_quotes_only_its_own_document(
    notice_client: httpx.AsyncClient,
) -> None:
    case_id, headers = await _case_with_documents(
        notice_client, [("fatura_loja.pdf", FATURA), ("contrato_xpto.pdf", CONTRATO)]
    )

    notice = (await notice_client.post(f"/consumer/cases/{case_id}/notice", headers=headers)).json()

    assert notice["evidence_references"]
    for citation in notice["evidence_references"]:
        own, foreign = (
            ("LOJA EXEMPLO", "OPERADORA XPTO")
            if citation["filename"] == "fatura_loja.pdf"
            else ("OPERADORA XPTO", "LOJA EXEMPLO")
        )
        assert own in citation["quote"]
        assert foreign not in citation["quote"]


async def test_internal_page_marker_never_reaches_the_notice(
    notice_client: httpx.AsyncClient,
) -> None:
    """The marker carries the case id and is scaffolding, not evidence."""
    case_id, headers = await _case_with_documents(
        notice_client, [("fatura_loja.pdf", FATURA), ("contrato_xpto.pdf", CONTRATO)]
    )

    notice = (await notice_client.post(f"/consumer/cases/{case_id}/notice", headers=headers)).json()
    markdown = (
        await notice_client.get(f"/consumer/cases/{case_id}/notice.md", headers=headers)
    ).text

    assert all("EVIDENCIA" not in item["quote"] for item in notice["evidence_references"])
    assert "EVIDENCIA" not in markdown
    assert case_id[:8].upper() not in markdown


def test_evidence_page_marker_is_always_recognised_as_a_heading() -> None:
    """Section detection is what keeps one chunk inside one evidence page.

    The marker embeds two uuid4 hex fragments. In lower case those carry a-f,
    which pushed the upper-case ratio below the heading threshold for ~97% of
    random ids, so pages silently merged into a single section.
    """
    import uuid

    for _ in range(200):
        marker = (
            f"CASO {uuid.uuid4().hex[:8].upper()} EVIDENCIA "
            f"{uuid.uuid4().hex[:8].upper()} PAGINA 1"
        )
        assert is_heading(marker), marker


def test_chunk_spanning_pages_is_dropped_rather_than_misattributed() -> None:
    """The guard must not depend on the heading heuristic staying correct."""
    from app.consumer.schemas import ConsumerEvidence, EvidenceStatus
    from app.consumer.service import ConsumerCaseService
    from app.consumer.store import StoredEvidence
    from app.schemas.document import ExtractionMethod
    from app.schemas.rag import Chunk, RetrievedChunk

    def _evidence(name: str) -> StoredEvidence:
        return StoredEvidence(
            public=ConsumerEvidence(
                evidence_id="a" * 32,
                filename=name,
                page_count=1,
                media_type="application/pdf",
                extraction_method=ExtractionMethod.NATIVE_TEXT,
                status=EvidenceStatus.ACCEPTED,
                source_sha256="0" * 64,
                content_sha256="1" * 64,
            )
        )

    page_sources = {1: (_evidence("a.pdf"), 1), 2: (_evidence("b.pdf"), 1)}
    spanning = RetrievedChunk(
        chunk=Chunk(
            chunk_id="doc:0000",
            doc_id="doc",
            text="Texto da pagina um. Texto da pagina dois.",
            page_start=1,
            page_end=2,
        ),
        score=0.9,
    )

    result_sets = [[spanning]]
    assert (
        ConsumerCaseService._evidence_references(
            result_sets,
            page_sources,
            _evidence_traces(result_sets),
        )
        == []
    )


def test_evidence_references_exclude_hits_without_retrieval_support() -> None:
    from app.consumer.schemas import ConsumerEvidence, EvidenceStatus
    from app.consumer.service import ConsumerCaseService
    from app.consumer.store import StoredEvidence
    from app.schemas.document import ExtractionMethod
    from app.schemas.rag import Chunk

    def _evidence(evidence_id: str, name: str) -> StoredEvidence:
        return StoredEvidence(
            public=ConsumerEvidence(
                evidence_id=evidence_id,
                filename=name,
                page_count=1,
                media_type="application/pdf",
                extraction_method=ExtractionMethod.NATIVE_TEXT,
                status=EvidenceStatus.ACCEPTED,
                source_sha256="0" * 64,
                content_sha256="1" * 64,
            )
        )

    relevant = RetrievedChunk(
        chunk=Chunk(
            chunk_id="evidence:relevant",
            doc_id="evidence-doc",
            text="Compra cobrada na fatura sem autorização.",
            page_start=1,
            page_end=1,
        ),
        score=0.032,
    )
    unrelated = RetrievedChunk(
        chunk=Chunk(
            chunk_id="evidence:unrelated",
            doc_id="evidence-doc",
            text="Cardápio do restaurante anexo.",
            page_start=2,
            page_end=2,
        ),
        score=0.016,
    )
    result_sets = [[relevant, unrelated]]
    page_sources = {
        1: (_evidence("a" * 32, "fatura.pdf"), 1),
        2: (_evidence("b" * 32, "anexo.pdf"), 1),
    }

    citations = ConsumerCaseService._evidence_references(
        result_sets,
        page_sources,
        _evidence_traces(result_sets),
    )

    assert [(citation.filename, citation.chunk_id) for citation in citations] == [
        ("fatura.pdf", "evidence:relevant")
    ]


def test_untrusted_excerpt_cannot_inject_a_markdown_link() -> None:
    raw = "Veja [clique aqui](https://evil.example/phishing) para detalhes."

    escaped = _markdown_inline(raw)

    assert "\\[clique aqui\\]" in escaped
    assert "](https://evil.example" not in escaped.replace("\\]", "]REMOVED")
    # PDF and DOCX are not Markdown, so they show the literal original text.
    assert strip_markdown_escapes(escaped) == raw


def test_clean_chunk_quote_strips_the_marker_anywhere_in_the_text() -> None:
    text = (
        "CASO A1B2C3D4 EVIDENCIA 99887766 PAGINA 1\n"
        "Cobranca de R$ 10,00 nao reconhecida."
    )

    assert _clean_chunk_quote(text) == "Cobranca de R$ 10,00 nao reconhecida."


async def test_lexical_only_retrieval_is_declared_in_the_notice(
    notice_client: httpx.AsyncClient, monkeypatch
) -> None:
    """A draft built without semantic retrieval must say so where a reader looks.

    Degradation used to live only inside the per-query audit records, so a
    lexical-only notice was indistinguishable from a hybrid one in both the API
    response and the UI.
    """
    from app.rag import pipeline as pipeline_module
    from app.rag.resilience import EmbeddingUnavailableError

    case_id, headers = await _case_with_documents(notice_client, [("fatura.pdf", FATURA)])

    async def _unavailable(self, queries):  # noqa: ANN001, ANN202
        raise EmbeddingUnavailableError("embedding circuit breaker is open")

    monkeypatch.setattr(
        pipeline_module.QueryEmbeddingGuard, "embed", _unavailable, raising=True
    )
    notice = (await notice_client.post(f"/consumer/cases/{case_id}/notice", headers=headers)).json()

    assert notice["retrieval_degraded_modes"] == ["lexical_only"]
    assert any("busca semântica" in warning for warning in notice["warnings"])


async def test_delivered_notice_carries_no_retrieval_identifiers(
    notice_client: httpx.AsyncClient,
) -> None:
    """Chunk ids, corpus release and hashes are provenance, not letter content.

    They belong to the consumer's audit surface. Addressed to the supplier they
    are noise, and the per-ground policy commentary actively undercuts the
    notice by explaining that software picked the article.
    """
    case_id, headers = await _case_with_documents(notice_client, [("fatura.pdf", FATURA)])

    notice = (await notice_client.post(f"/consumer/cases/{case_id}/notice", headers=headers)).json()
    markdown = notice["full_text"]

    assert "chunk `" not in markdown
    assert "SHA-256" not in markdown
    assert "corpus `" not in markdown
    assert "política de recuperação" not in markdown
    assert "não foi decidida pelo sistema" not in markdown
    # The same provenance stays available where auditors look for it.
    assert all(item["chunk_id"] for item in notice["evidence_references"])
    assert all(len(item["content_sha256"]) == 64 for item in notice["evidence_references"])
    assert notice["corpus_sha256"]
    assert all(
        ground["authority"]["corpus_release_id"] for ground in notice["legal_grounds"]
    )
    assert all(
        ground["application_to_facts"] for ground in notice["legal_grounds"]
    )


async def test_notice_has_addressing_and_signature_blocks(
    notice_client: httpx.AsyncClient,
) -> None:
    case_id, headers = await _case_with_documents(notice_client, [("fatura.pdf", FATURA)])

    markdown = (
        await notice_client.post(f"/consumer/cases/{case_id}/notice", headers=headers)
    ).json()["full_text"]

    assert "**À**" in markdown
    assert "[PREENCHER ENDEREÇO DA NOTIFICADA]" in markdown
    assert "[PREENCHER LOCAL], [PREENCHER DATA]." in markdown
    assert "[PREENCHER CPF DO(A) NOTIFICANTE]" in markdown


def test_multiline_request_stays_one_list_item() -> None:
    """A newline inside a bullet silently drops the marker for the rest."""
    from app.consumer.schemas import ConsumerCaseFacts, ConsumerIssueCategory
    from app.consumer.service import _render_notice_markdown, _requests

    facts = ConsumerCaseFacts(
        consumer_name="Gabriel",
        bank_name="Bradesco",
        issue_category=ConsumerIssueCategory.UNAUTHORIZED_CHARGE,
        complaint_summary="Cobrancas indevidas.",
        incident_date_or_period="09/06/2026",
        desired_resolution="ressarcimento do valor cobrado\nretirada de negativacao",
    )

    markdown = _render_notice_markdown(
        facts=facts,
        evidence=[],
        legal_grounds=[],
        requests=_requests(facts),
        public_proposal=None,
    )
    section = markdown[markdown.index("## 5.") : markdown.index("## 6.")]
    items = [line for line in section.splitlines() if line.strip()][1:]

    assert all(line.startswith("- ") for line in items)
    assert "- ressarcimento do valor cobrado retirada de negativacao" in items


def test_masked_identifiers_survive_untouched_in_the_delivered_text() -> None:
    """Escaping emphasis mangled every masked CPF/CNPJ in real evidence."""
    masked = "CPF/CNPJ: 18.*** ***/8000-43"

    assert _markdown_inline(masked) == masked
