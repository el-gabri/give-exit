"""Tests for the ingestion pipeline (offline, no OCR binary required)."""

import struct
import zlib
from pathlib import Path

import fitz
import pytest

from app.ingestion.pdf_reader import (
    _image_dimensions_from_header,
    extract_text,
    image_dimensions,
)
from app.ingestion.service import (
    MAX_IMAGE_PIXELS,
    DocumentIngestionService,
    DocumentTextUnavailableError,
)
from app.schemas.document import ExtractionMethod

PT_TEXT = (
    "EXCELENTISSIMO SENHOR DOUTOR JUIZ DE DIREITO DA 3a VARA CIVEL. "
    "Maria Silva, brasileira, portadora do CPF 000.000.000-00, vem, "
    "por seu advogado, propor a presente acao de indenizacao por danos "
    "morais e materiais em face de Banco Exemplo S.A., pelos fatos e "
    "fundamentos juridicos a seguir expostos. Da tutela de urgencia. "
    "Do valor da causa: R$ 50.000,00."
)


def _make_pdf(path: Path, page_texts: list[str]) -> Path:
    doc = fitz.open()
    for text in page_texts:
        page = doc.new_page()
        if text:
            # insert_textbox wraps lines; insert_text would clip at the
            # page edge and silently truncate the fixture content.
            rect = fitz.Rect(72, 72, page.rect.width - 72, page.rect.height - 72)
            page.insert_textbox(rect, text, fontsize=11)
    doc.save(path)
    doc.close()
    return path


def _make_png(path: Path) -> Path:
    document = fitz.open()
    page = document.new_page(width=640, height=480)
    page.insert_text(
        fitz.Point(40, 80),
        "Comprovante de compra e protocolo de atendimento.",
    )
    page.get_pixmap().save(str(path))
    document.close()
    return path


def _make_jpeg(path: Path) -> Path:
    document = fitz.open()
    page = document.new_page(width=640, height=480)
    page.insert_text(fitz.Point(40, 80), "Comprovante de compra.")
    page.get_pixmap().save(str(path))
    document.close()
    return path


def _rewrite_png_dimensions(path: Path, width: int, height: int) -> None:
    payload = bytearray(path.read_bytes())
    payload[16:24] = struct.pack(">II", width, height)
    payload[29:33] = struct.pack(">I", zlib.crc32(payload[12:29]))
    path.write_bytes(payload)


class FakeOcr:
    """Deterministic OCR double."""

    def ocr_image(self, image_png: bytes) -> str:
        return "texto reconhecido via OCR"


@pytest.fixture
def text_pdf(tmp_path: Path) -> Path:
    return _make_pdf(tmp_path / "consumer-evidence.pdf", [PT_TEXT, PT_TEXT])


@pytest.fixture
def scanned_pdf(tmp_path: Path) -> Path:
    # No text layer at all -> looks like a scan
    return _make_pdf(tmp_path / "scan.pdf", ["", ""])


@pytest.fixture
def evidence_png(tmp_path: Path) -> Path:
    return _make_png(tmp_path / "evidence.png")


async def test_ingests_native_text_pdf(text_pdf: Path) -> None:
    service = DocumentIngestionService()
    doc = await service.ingest(text_pdf)

    assert doc.page_count == 2
    assert doc.extraction_method is ExtractionMethod.NATIVE_TEXT
    assert not doc.ocr_applied
    assert doc.language == "pt"
    assert "danos" in doc.full_text
    assert doc.warnings == []


async def test_scanned_pdf_uses_ocr_when_engine_available(scanned_pdf: Path) -> None:
    service = DocumentIngestionService(ocr_engine=FakeOcr())
    doc = await service.ingest(scanned_pdf)

    assert doc.extraction_method is ExtractionMethod.OCR
    assert doc.ocr_applied
    assert "OCR" in doc.full_text


async def test_scanned_pdf_without_ocr_engine_warns(scanned_pdf: Path) -> None:
    service = DocumentIngestionService(ocr_engine=None)
    doc = await service.ingest(scanned_pdf)

    assert doc.extraction_method is ExtractionMethod.NATIVE_TEXT
    assert not doc.ocr_applied
    assert len(doc.warnings) == 1


async def test_image_uses_ocr_when_engine_available(evidence_png: Path) -> None:
    service = DocumentIngestionService(ocr_engine=FakeOcr())

    document = await service.ingest(evidence_png)

    assert document.page_count == 1
    assert document.extraction_method is ExtractionMethod.OCR
    assert document.ocr_applied
    assert document.full_text == "texto reconhecido via OCR"


async def test_image_without_ocr_is_rejected(evidence_png: Path) -> None:
    service = DocumentIngestionService(ocr_engine=None)

    with pytest.raises(DocumentTextUnavailableError, match="configure o OCR"):
        await service.ingest(evidence_png)


async def test_image_with_empty_ocr_result_is_rejected(evidence_png: Path) -> None:
    class EmptyOcr:
        def ocr_image(self, image_png: bytes) -> str:
            return "   "

    service = DocumentIngestionService(ocr_engine=EmptyOcr())

    with pytest.raises(DocumentTextUnavailableError, match="extrair texto"):
        await service.ingest(evidence_png)


async def test_image_pixel_limit_is_enforced(evidence_png: Path, monkeypatch) -> None:
    def reject_oversized(_: Path, *, max_pixels: int) -> tuple[int, int]:
        assert max_pixels == MAX_IMAGE_PIXELS
        raise ValueError("Image exceeds the pixel safety limit")

    monkeypatch.setattr(
        "app.ingestion.service.pdf_reader.image_dimensions",
        reject_oversized,
    )
    service = DocumentIngestionService(ocr_engine=FakeOcr())

    with pytest.raises(ValueError, match="pixel safety limit"):
        await service.ingest(evidence_png)


async def test_hybrid_pdf_uses_ocr_only_for_scanned_pages(tmp_path: Path) -> None:
    hybrid_pdf = _make_pdf(tmp_path / "hybrid.pdf", [PT_TEXT, ""])
    service = DocumentIngestionService(ocr_engine=FakeOcr())

    doc = await service.ingest(hybrid_pdf)

    assert doc.extraction_method is ExtractionMethod.HYBRID
    assert "danos" in doc.pages[0].text
    assert doc.pages[1].text == "texto reconhecido via OCR"


async def test_rejects_pdf_above_page_limit(text_pdf: Path) -> None:
    service = DocumentIngestionService(max_pages=1)

    with pytest.raises(ValueError, match="page limit"):
        await service.ingest(text_pdf)


def test_needs_ocr_heuristic(text_pdf: Path, scanned_pdf: Path) -> None:
    assert extract_text(text_pdf).needs_ocr is False
    assert extract_text(scanned_pdf).needs_ocr is True


def test_doc_id_is_content_addressed(text_pdf: Path, tmp_path: Path) -> None:
    copy = _make_pdf(tmp_path / "copy.pdf", [PT_TEXT, PT_TEXT])
    service = DocumentIngestionService()
    import asyncio

    doc1 = asyncio.run(service.ingest(text_pdf))
    doc2 = asyncio.run(service.ingest(copy))
    assert doc1.doc_id == doc2.doc_id  # same content, same id (idempotency)


def test_missing_file_raises() -> None:
    with pytest.raises(FileNotFoundError):
        extract_text(Path("does/not/exist.pdf"))


async def test_scanned_pdf_without_ocr_is_rejected_when_text_required(
    scanned_pdf: Path,
) -> None:
    service = DocumentIngestionService(ocr_engine=None)

    with pytest.raises(DocumentTextUnavailableError, match="configure o OCR"):
        await service.ingest(scanned_pdf, require_text=True)


async def test_scanned_pdf_with_noisy_ocr_is_rejected_when_text_required(
    scanned_pdf: Path,
) -> None:
    class NoisyOcr:
        def ocr_image(self, image_png: bytes) -> str:
            return "|||| 1111 x x x x"

    service = DocumentIngestionService(ocr_engine=NoisyOcr())

    with pytest.raises(DocumentTextUnavailableError, match="texto suficiente"):
        await service.ingest(scanned_pdf, require_text=True)


async def test_image_with_noisy_ocr_result_is_rejected(
    evidence_png: Path,
) -> None:
    class NoisyOcr:
        def ocr_image(self, image_png: bytes) -> str:
            return "... ::: 12345 x x x"

    service = DocumentIngestionService(ocr_engine=NoisyOcr())

    with pytest.raises(DocumentTextUnavailableError, match="texto suficiente"):
        await service.ingest(evidence_png)


def test_image_dimensions_are_checked_from_metadata_before_decode(
    evidence_png: Path,
) -> None:
    _rewrite_png_dimensions(evidence_png, MAX_IMAGE_PIXELS, 2)

    with pytest.raises(ValueError, match="pixel safety limit"):
        image_dimensions(evidence_png, max_pixels=MAX_IMAGE_PIXELS)


def test_header_fallback_reads_png_and_jpeg_dimensions(tmp_path: Path) -> None:
    png = _make_png(tmp_path / "fallback.png")
    jpeg = _make_jpeg(tmp_path / "fallback.jpg")

    assert _image_dimensions_from_header(png) == (640, 480)
    assert _image_dimensions_from_header(jpeg) == (640, 480)


async def test_scanned_pdf_with_failing_ocr_is_rejected_when_text_required(
    scanned_pdf: Path,
) -> None:
    class FailingOcr:
        def ocr_image(self, image_png: bytes) -> str:
            raise RuntimeError("OCR unavailable")

    service = DocumentIngestionService(ocr_engine=FailingOcr())

    with pytest.raises(DocumentTextUnavailableError, match="texto suficiente"):
        await service.ingest(scanned_pdf, require_text=True)
