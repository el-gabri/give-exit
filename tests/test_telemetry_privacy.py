"""Privacy regression tests for values emitted to operational telemetry."""

from pathlib import Path

import httpx

from app.enrichment.datajud import DataJudClient
from app.ingestion.pdf_reader import PdfExtraction
from app.ingestion.service import DocumentIngestionService
from app.security.telemetry import TelemetryRedactor

CASE_FORMATTED = "0001234-56.2024.8.26.0100"
CASE_DIGITS = "00012345620248260100"


class _RecordingLogger:
    def __init__(self) -> None:
        self.events: list[tuple[str, dict[str, object]]] = []

    def info(self, event: str, **values: object) -> None:
        self.events.append((event, values))

    def warning(self, event: str, **values: object) -> None:
        self.events.append((event, values))


def test_masks_brazilian_identifiers_email_and_phone_deterministically() -> None:
    raw_values = (
        "123.456.789-00",
        "12.345.678/0001-90",
        CASE_FORMATTED,
        "maria.silva@example.com",
        "+55 (11) 98765-4321",
    )
    source = " | ".join(raw_values)
    redactor = TelemetryRedactor("test-key")

    first = redactor.redact_text(source)
    second = redactor.redact_text(source)

    assert first == second
    assert "[CPF_REDACTED]" in first
    assert "[CNPJ_REDACTED]" in first
    assert "[CNJ_REDACTED]" in first
    assert "[EMAIL_REDACTED]" in first
    assert "[PHONE_REDACTED]" in first
    assert all(raw not in first for raw in raw_values)


def test_configured_pseudonyms_are_stable_and_keyed() -> None:
    first = TelemetryRedactor("shared-secret")
    second = TelemetryRedactor("shared-secret")
    other_key = TelemetryRedactor("different-secret")

    formatted_reference = first.case_reference(CASE_FORMATTED)

    assert formatted_reference == second.case_reference(CASE_DIGITS)
    assert formatted_reference != other_key.case_reference(CASE_FORMATTED)
    assert CASE_FORMATTED not in formatted_reference
    assert CASE_DIGITS not in formatted_reference


def test_filename_reference_retains_only_safe_suffix() -> None:
    raw_filename = "caso 123.456.789-00 maria@example.com.PDF"

    reference = TelemetryRedactor("test-key").filename_reference(raw_filename)

    assert reference.startswith("file_")
    assert reference.endswith(".pdf")
    assert "123.456.789-00" not in reference
    assert "maria@example.com" not in reference


async def test_datajud_logs_pseudonym_not_case_number(monkeypatch) -> None:
    recording_logger = _RecordingLogger()
    monkeypatch.setattr("app.enrichment.datajud.logger", recording_logger)

    def not_found(_: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"hits": {"hits": []}})

    client = DataJudClient(
        base_url="https://api.example",
        api_key="public-key",
        transport=httpx.MockTransport(not_found),
        telemetry_redactor=TelemetryRedactor("test-key"),
    )

    await client.lookup(CASE_FORMATTED)

    assert len(recording_logger.events) == 1
    event, values = recording_logger.events[0]
    assert event == "datajud_not_found"
    assert "case_ref" in values
    assert "case" not in values
    serialized = repr(recording_logger.events)
    assert CASE_FORMATTED not in serialized
    assert CASE_DIGITS not in serialized


async def test_ingestion_logs_filename_reference_not_raw_filename(monkeypatch) -> None:
    recording_logger = _RecordingLogger()
    monkeypatch.setattr("app.ingestion.service.logger", recording_logger)
    monkeypatch.setattr(
        "app.ingestion.service.pdf_reader.extract_text",
        lambda _path, max_pages=None: PdfExtraction(
            page_texts=["Documento com texto suficiente para análise segura."],
            page_needs_ocr=[False],
        ),
    )
    raw_filename = "caso 123.456.789-00 maria@example.com.pdf"
    service = DocumentIngestionService(
        telemetry_redactor=TelemetryRedactor("test-key")
    )

    document = await service.ingest(Path(raw_filename))

    assert document.filename == raw_filename
    assert len(recording_logger.events) == 1
    event, values = recording_logger.events[0]
    assert event == "document_ingested"
    assert "file_ref" in values
    assert "file" not in values
    serialized = repr(recording_logger.events)
    assert raw_filename not in serialized
    assert "123.456.789-00" not in serialized
    assert "maria@example.com" not in serialized
