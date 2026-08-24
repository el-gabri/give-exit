"""Tests for runtime citation validation before report delivery."""

import pytest

from app.schemas.analysis import LawsuitClassification, TimelineEvent
from app.schemas.common import Citation, ConfidentConclusion
from app.schemas.document import DocumentPage, ExtractionMethod, ParsedDocument
from app.schemas.lawsuit import LawsuitType
from app.schemas.rag import Chunk
from app.schemas.report import LitigationReport
from app.services.citations import is_substantive_quote, validate_report_citations


def _document() -> ParsedDocument:
    return ParsedDocument(
        filename="peticao.pdf",
        pages=[
            DocumentPage(number=1, text="A autora relata cobrancas indevidas."),
            DocumentPage(number=2, text="Requer indenizacao por danos morais."),
        ],
        language="pt",
        extraction_method=ExtractionMethod.NATIVE_TEXT,
    )


def _report(document: ParsedDocument) -> LitigationReport:
    return LitigationReport(
        doc_id=document.doc_id,
        filename=document.filename,
        language=document.language,
        executive_summary="Resumo.",
        classification=LawsuitClassification(
            lawsuit_type=LawsuitType.CONSUMER,
            conclusion=ConfidentConclusion(
                statement="Acao de consumo.",
                confidence=0.8,
                reasoning="Cobrancas bancarias.",
                citations=[
                    Citation(
                        chunk_id=f"{document.doc_id}:0000",
                        quote="texto inventado pelo modelo",
                        page=2,
                    ),
                    Citation(chunk_id=f"{document.doc_id}:nao-existe"),
                ],
            ),
        ),
    )


def test_reconstructs_quote_and_page_from_a_valid_chunk_id() -> None:
    document = _document()
    chunk = Chunk(
        chunk_id=f"{document.doc_id}:0000",
        doc_id=document.doc_id,
        text="A autora relata cobrancas indevidas.",
        page_start=1,
        page_end=1,
    )
    report = _report(document)

    result = validate_report_citations(report, document, [chunk])

    assert result.total_citations == 2
    assert result.verified_citations == 1
    assert result.rejected_citations == 1
    assert report.classification is not None
    assert report.classification.conclusion.citations == [
        Citation(
            chunk_id=f"{document.doc_id}:0000",
            quote="A autora relata cobrancas indevidas",
            page=1,
        )
    ]


def test_citation_schema_requires_only_the_chunk_id_from_the_model() -> None:
    schema = Citation.model_json_schema()

    assert schema["required"] == ["chunk_id"]
    assert Citation(chunk_id="doc:0001").quote == ""
    assert Citation(chunk_id="doc:0001").page is None
    with pytest.raises(ValueError):
        Citation()  # type: ignore[call-arg]


def test_rejects_an_unknown_chunk_id_even_with_a_real_quote() -> None:
    document = _document()
    report = _report(document)
    assert report.classification is not None
    report.classification.conclusion.citations = [
        Citation(
            chunk_id=f"{document.doc_id}:nao-existe",
            quote="indenizacao por danos morais",
            page=2,
        )
    ]

    result = validate_report_citations(report, document, [])

    assert result.rejected_citations == 1
    assert result.conclusions_without_verified_citation == 1
    assert report.classification.conclusion.citations == []


def test_rejects_a_quote_too_short_to_be_evidence() -> None:
    document = _document()
    report = _report(document)
    assert report.classification is not None
    chunk = Chunk(
        chunk_id=f"{document.doc_id}:curto",
        doc_id=document.doc_id,
        text="a autora",
        page_start=1,
        page_end=1,
    )
    report.classification.conclusion.citations = [Citation(chunk_id=chunk.chunk_id)]

    result = validate_report_citations(report, document, [chunk])

    assert result.rejected_citations == 1
    assert report.classification.conclusion.citations == []


def test_substantive_quote_rule_covers_length_and_word_count() -> None:
    # Two-word legal terms are real evidence and must survive the floor.
    assert is_substantive_quote("cobrancas indevidas")
    assert is_substantive_quote("danos morais")
    assert not is_substantive_quote("indenizacao")  # single word
    assert not is_substantive_quote("a autora")  # two words, too few characters
    assert not is_substantive_quote("   ")


def test_rejects_an_invalid_timeline_citation() -> None:
    document = _document()
    report = _report(document)
    assert report.classification is not None
    report.classification.conclusion.citations = []
    report.timeline = [
        TimelineEvent(
            date="2025-01-10",
            description="Pedido de indenizacao",
            citation=Citation(chunk_id=f"{document.doc_id}:nao-existe"),
        )
    ]

    result = validate_report_citations(report, document, [])

    assert result.total_citations == 1
    assert result.rejected_citations == 1
    assert report.timeline[0].citation is None


def test_reconstructs_a_timeline_citation_from_the_source_page() -> None:
    document = _document()
    report = _report(document)
    assert report.classification is not None
    report.classification.conclusion.citations = []
    chunk = Chunk(
        chunk_id=f"{document.doc_id}:0001",
        doc_id=document.doc_id,
        text="Requer indenizacao por danos morais.",
        page_start=2,
        page_end=2,
    )
    report.timeline = [
        TimelineEvent(
            description="Pedido de indenizacao",
            citation=Citation(
                chunk_id=chunk.chunk_id,
                quote="conteudo nao confiavel",
                page=1,
            ),
        )
    ]

    result = validate_report_citations(report, document, [chunk])

    assert result.verified_citations == 1
    assert report.timeline[0].citation == Citation(
        chunk_id=chunk.chunk_id,
        quote="Requer indenizacao por danos morais",
        page=2,
    )


@pytest.mark.parametrize("failure", ["foreign_document", "no_source_overlap", "duplicate_id"])
def test_rejects_unverifiable_chunk_provenance(failure: str) -> None:
    document = _document()
    report = _report(document)
    assert report.classification is not None
    chunk_id = f"{document.doc_id}:0000"
    base = Chunk(
        chunk_id=chunk_id,
        doc_id=document.doc_id,
        text=document.pages[0].text,
        page_start=1,
        page_end=1,
    )
    chunks = [base]
    if failure == "foreign_document":
        chunks = [base.model_copy(update={"doc_id": "foreign"})]
    elif failure == "no_source_overlap":
        chunks = [base.model_copy(update={"text": "conteudo totalmente diferente"})]
    else:
        chunks.append(base.model_copy(update={"text": "segunda versao ambigua"}))
    report.classification.conclusion.citations = [Citation(chunk_id=chunk_id)]

    result = validate_report_citations(report, document, chunks)

    assert result.rejected_citations == 1
    assert report.classification.conclusion.citations == []
