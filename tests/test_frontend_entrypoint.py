"""Regression tests for the documented Streamlit entrypoint."""

from pathlib import Path

import pytest
import requests
from streamlit.testing.v1 import AppTest

from frontend.api_client import ConsumerApiClient, ConsumerApiError
from frontend.consumer_view import _financial_source_rows


def test_streamlit_app_loads_from_frontend_script_path(monkeypatch) -> None:
    """The documented command must resolve sibling frontend modules."""
    monkeypatch.setenv("LITIGATION_API_URL", "http://127.0.0.1:1")
    app_path = Path(__file__).parents[1] / "frontend" / "streamlit_app.py"

    app = AppTest.from_file(app_path).run(timeout=10)

    assert not app.exception


def test_consumer_view_uses_supplier_neutral_title(monkeypatch) -> None:
    monkeypatch.setenv("LITIGATION_API_URL", "http://127.0.0.1:1")
    app_path = Path(__file__).parents[1] / "frontend" / "streamlit_app.py"
    app = AppTest.from_file(app_path).run(timeout=10)

    assert not app.exception
    titles = [element.value for element in app.title]
    assert "Assistente para reclamações" in titles
    assert all("bancárias" not in title.casefold() for title in titles)


def test_consumer_form_prioritizes_summary_solution_and_image_evidence() -> None:
    source = (Path(__file__).parents[1] / "frontend" / "consumer_view.py").read_text(
        encoding="utf-8"
    )

    assert '"Resumo do ocorrido"' in source
    assert '"Qual solução você espera?"' in source
    assert '"Detalhes adicionais (opcional)"' in source
    assert '"Compensação adicional pretendida (R$)"' not in source
    assert 'type=["pdf", "png", "jpg", "jpeg"]' in source


def test_financial_source_rows_expose_document_provenance() -> None:
    rows = _financial_source_rows(
        {
            "components": [
                {
                    "kind": "direct_loss",
                    "amount": "250.00",
                    "included_in_public_proposal": True,
                    "sources": [
                        {
                            "source_type": "evidence",
                            "filename": "nota.png",
                            "page": 1,
                            "quote": "Total pago: R$ 250,00",
                            "source_sha256": "a" * 64,
                            "content_sha256": "b" * 64,
                            "quote_sha256": "c" * 64,
                            "extraction_method": "ocr",
                            "ocr_applied": True,
                        }
                    ],
                }
            ]
        }
    )

    assert rows[0]["Origem"] == "Documento"
    assert rows[0]["Arquivo/página"] == "nota.png, p. 1"
    assert rows[0]["SHA-256 do arquivo"] == "a" * 64
    assert rows[0]["Extração"] == "ocr + OCR"


def test_consumer_client_forwards_configured_api_key(monkeypatch) -> None:
    captured: dict = {}

    def request(method, url, **kwargs):
        captured.update(method=method, url=url, **kwargs)
        response = requests.Response()
        response.status_code = 200
        response._content = b"{}"
        return response

    monkeypatch.setattr("frontend.api_client.requests.request", request)

    ConsumerApiClient("https://api.example", api_key="shared-secret").health()

    assert captured["headers"]["X-API-Key"] == "shared-secret"


def test_consumer_client_distinguishes_timeout_from_connection_failure(monkeypatch) -> None:
    def request(*args, **kwargs):
        raise requests.Timeout("deadline exceeded")

    monkeypatch.setattr("frontend.api_client.requests.request", request)

    with pytest.raises(ConsumerApiError, match="excedeu o tempo de espera"):
        ConsumerApiClient("https://api.example").generate_notice("case", "token")
