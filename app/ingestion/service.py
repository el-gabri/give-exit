"""Ingestion use case: PDF or image file -> ParsedDocument.

Flow:
    native text extraction
        -> looks like a scan? -> OCR (if engine available, else warn)
        -> language detection
        -> ParsedDocument

CPU/disk-bound work runs in a worker thread (``asyncio.to_thread``) so the
async API event loop is never blocked by a large document.
"""

import asyncio
import re
from pathlib import Path

from app.core.logging import get_logger
from app.ingestion import pdf_reader
from app.ingestion.language import detect_language
from app.ingestion.ocr import OcrEngine
from app.schemas.document import DocumentPage, ExtractionMethod, ParsedDocument
from app.security.telemetry import TelemetryRedactor

logger = get_logger(__name__)
SUPPORTED_IMAGE_SUFFIXES = frozenset({".png", ".jpg", ".jpeg"})
MAX_IMAGE_PIXELS = 40_000_000
MIN_OCR_ALNUM_CHARS = 16
MIN_OCR_ALPHA_CHARS = 10
MIN_OCR_WORDS = 3


class DocumentTextUnavailableError(ValueError):
    """Raised when evidence cannot produce text safe for automated analysis."""


_OCR_TOKEN_RE = re.compile(r"[^\W_]+", flags=re.UNICODE)


def _has_usable_ocr_text(text: str) -> bool:
    """Apply a conservative structural quality floor to OCR output."""
    tokens = [token for token in _OCR_TOKEN_RE.findall(text) if len(token) >= 2]
    alnum_chars = sum(character.isalnum() for character in text)
    alpha_chars = sum(character.isalpha() for character in text)
    distinct_tokens = {token.casefold() for token in tokens}
    return (
        alnum_chars >= MIN_OCR_ALNUM_CHARS
        and alpha_chars >= MIN_OCR_ALPHA_CHARS
        and len(tokens) >= MIN_OCR_WORDS
        and len(distinct_tokens) >= 2
    )


class DocumentIngestionService:
    """Turn an uploaded PDF or image into a ParsedDocument."""

    def __init__(
        self,
        ocr_engine: OcrEngine | None = None,
        max_pages: int | None = None,
        telemetry_redactor: TelemetryRedactor | None = None,
    ) -> None:
        self._ocr_engine = ocr_engine
        self._max_pages = max_pages
        self._telemetry = telemetry_redactor or TelemetryRedactor()

    async def ingest(self, path: Path, *, require_text: bool = False) -> ParsedDocument:
        """Ingest a document, optionally rejecting incomplete OCR evidence.

        The flag is intentionally per call: business lawsuit analysis keeps
        its warning-based behavior, while consumer evidence can fail closed
        when scanned pages cannot be read.
        """
        return await asyncio.to_thread(self._ingest_sync, path, require_text=require_text)

    def _ingest_sync(self, path: Path, *, require_text: bool = False) -> ParsedDocument:
        file_ref = self._telemetry.filename_reference(path.name)
        is_image = path.suffix.casefold() in SUPPORTED_IMAGE_SUFFIXES
        if is_image:
            pdf_reader.image_dimensions(path, max_pixels=MAX_IMAGE_PIXELS)
        extraction = pdf_reader.extract_text(path, max_pages=self._max_pages)
        warnings: list[str] = []
        method = ExtractionMethod.NATIVE_TEXT
        ocr_applied = False
        page_texts = extraction.page_texts
        unresolved_ocr_indexes = set(extraction.ocr_page_indexes)

        if extraction.needs_ocr:
            if self._ocr_engine is not None:
                ocr_indexes = extraction.ocr_page_indexes
                logger.info("ocr_started", file_ref=file_ref, pages=len(ocr_indexes))
                images = (
                    [path.read_bytes()]
                    if is_image
                    else pdf_reader.render_page_images(path, page_indexes=ocr_indexes)
                )
                successful_pages = 0
                for index, image in zip(ocr_indexes, images, strict=True):
                    try:
                        recognized_text = self._ocr_engine.ocr_image(image)
                        if not _has_usable_ocr_text(recognized_text):
                            raise ValueError("OCR returned insufficient usable text")
                        page_texts[index] = recognized_text.strip()
                        successful_pages += 1
                        unresolved_ocr_indexes.discard(index)
                    except Exception as exc:
                        warnings.append(
                            f"OCR failed on page {index + 1}; extracted text may be incomplete."
                        )
                        logger.warning(
                            "ocr_page_failed",
                            file_ref=file_ref,
                            page=index + 1,
                            error=self._telemetry.redact_text(
                                exc, sensitive_values=(str(path), path.name)
                            ),
                        )
                if successful_pages:
                    method = (
                        ExtractionMethod.OCR
                        if successful_pages == len(page_texts)
                        else ExtractionMethod.HYBRID
                    )
                    ocr_applied = True
            else:
                warnings.append(
                    f"{len(extraction.ocr_page_indexes)} page(s) appear to be scanned but "
                    "no OCR engine is available; extracted text is likely incomplete."
                )
                logger.warning("ocr_needed_but_unavailable", file_ref=file_ref)

        if (is_image or require_text) and (
            unresolved_ocr_indexes or not any(text.strip() for text in page_texts)
        ):
            if self._ocr_engine is None:
                raise DocumentTextUnavailableError(
                    "Para analisar esta evidência, configure o OCR com suporte ao idioma português."
                )
            raise DocumentTextUnavailableError(
                "Não foi possível extrair texto suficiente via OCR. Envie uma imagem "
                "mais nítida ou um PDF pesquisável."
            )

        pages = [DocumentPage(number=i + 1, text=text) for i, text in enumerate(page_texts)]
        full_text = "\n\n".join(page_texts)
        document = ParsedDocument(
            filename=path.name,
            pages=pages,
            language=detect_language(full_text),
            extraction_method=method,
            ocr_applied=ocr_applied,
            warnings=warnings,
        )
        logger.info(
            "document_ingested",
            file_ref=file_ref,
            doc_id=document.doc_id,
            pages=document.page_count,
            language=document.language,
            method=method.value,
            warnings=len(warnings),
        )
        return document
