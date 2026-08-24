"""Tests for CNJ parsing, the DataJud client, and the enrichment node."""

import json
import typing

import httpx

from app.enrichment.cnj import normalize_case_number, tribunal_alias
from app.enrichment.datajud import DataJudClient
from app.enrichment.node import make_enrich_node
from app.orchestration.state import AnalysisState
from app.schemas.document import DocumentPage, ExtractionMethod, ParsedDocument
from app.schemas.lawsuit import LawsuitExtraction
from app.schemas.trace import AgentStatus

CASE_SP = "0001234-56.2024.8.26.0100"  # TJSP
CASE_DIGITS = "00012345620248260100"


def test_enrich_node_is_annotated_with_the_concrete_state_class() -> None:
    """LangGraph picks the argument form from the node's own annotation.

    Annotating the node with anything other than the concrete state class
    (an abstract base, a Protocol) silently hands it a plain dict, and every
    attribute read then fails at runtime instead of at type-check time.
    """
    node = make_enrich_node(None)

    assert typing.get_type_hints(node)["state"] is AnalysisState


def test_normalize_case_number() -> None:
    assert normalize_case_number(CASE_SP) == CASE_DIGITS
    assert normalize_case_number("processo n. " + CASE_SP) == CASE_DIGITS
    assert normalize_case_number("12345") is None


def test_tribunal_alias_derivation() -> None:
    assert tribunal_alias(CASE_SP) == "tjsp"
    assert tribunal_alias("0001234-56.2024.8.19.0001") == "tjrj"
    assert tribunal_alias("0001234-56.2024.8.07.0001") == "tjdft"  # DF special case
    assert tribunal_alias("0001234-56.2024.5.15.0001") == "trt15"
    assert tribunal_alias("0001234-56.2024.4.03.0001") == "trf3"
    assert tribunal_alias("0001234-56.2024.9.99.0001") is None  # unsupported


def _mock_transport(payload: dict, capture: dict) -> httpx.MockTransport:
    def handler(request: httpx.Request) -> httpx.Response:
        capture["url"] = str(request.url)
        capture["auth"] = request.headers.get("Authorization")
        capture["body"] = json.loads(request.content)
        return httpx.Response(200, json=payload)

    return httpx.MockTransport(handler)


DATAJUD_HIT = {
    "hits": {
        "hits": [
            {
                "_source": {
                    "numeroProcesso": CASE_DIGITS,
                    "tribunal": "TJSP",
                    "grau": "G1",
                    "classe": {"codigo": 7, "nome": "Procedimento Comum Civel"},
                    "orgaoJulgador": {"nome": "3a Vara Civel"},
                    "assuntos": [{"codigo": 1, "nome": "Dano Moral"}],
                    "dataAjuizamento": "2024-05-01T00:00:00.000Z",
                    "dataHoraUltimaAtualizacao": "2026-07-01T00:00:00.000Z",
                    "movimentos": [
                        {"codigo": 26, "nome": "Distribuicao", "dataHora": "2024-05-01"},
                        {"codigo": 51, "nome": "Audiencia", "dataHora": "2026-06-10"},
                    ],
                }
            }
        ]
    }
}


async def test_datajud_client_parses_hit_and_sends_auth() -> None:
    capture: dict = {}
    client = DataJudClient(
        base_url="https://api.example",
        api_key="PUBLIC_KEY",
        transport=_mock_transport(DATAJUD_HIT, capture),
    )
    alias, info = await client.lookup(CASE_SP)

    assert capture["url"] == "https://api.example/api_publica_tjsp/_search"
    assert capture["auth"] == "APIKey PUBLIC_KEY"
    assert capture["body"]["query"]["match"]["numeroProcesso"] == CASE_DIGITS
    assert alias == "tjsp"
    assert info is not None
    assert info.court_class == "Procedimento Comum Civel"
    assert info.movement_count == 2
    assert info.latest_movement.name == "Audiencia"


async def test_datajud_client_handles_no_hits() -> None:
    client = DataJudClient(
        base_url="https://api.example",
        api_key="k",
        transport=_mock_transport({"hits": {"hits": []}}, {}),
    )
    alias, info = await client.lookup(CASE_SP)
    assert alias == "tjsp"
    assert info is None


def _state(case_number: str | None) -> AnalysisState:
    return AnalysisState(
        document=ParsedDocument(
            filename="x.pdf",
            pages=[DocumentPage(number=1, text="conteudo")],
            language="pt",
            extraction_method=ExtractionMethod.NATIVE_TEXT,
        ),
        extraction=LawsuitExtraction(case_number=case_number),
    )


async def test_enrich_node_without_client_is_graceful() -> None:
    node = make_enrich_node(None)
    update = await node(_state(CASE_SP))
    enrichment = update["enrichment"]
    assert enrichment.attempted is False
    assert not enrichment.found
    assert update["traces"][0].agent == "datajud_enrichment"


async def test_enrich_node_without_case_number_is_graceful() -> None:
    client = DataJudClient(
        base_url="https://api.example",
        api_key="k",
        transport=_mock_transport(DATAJUD_HIT, {}),
    )
    update = await make_enrich_node(client)(_state(None))
    assert update["enrichment"].attempted is False


async def test_enrich_node_found() -> None:
    client = DataJudClient(
        base_url="https://api.example",
        api_key="k",
        transport=_mock_transport(DATAJUD_HIT, {}),
    )
    update = await make_enrich_node(client)(_state(CASE_SP))
    enrichment = update["enrichment"]
    assert enrichment.found is True
    assert enrichment.tribunal_alias == "tjsp"
    assert enrichment.info.tribunal == "TJSP"


async def test_enrich_node_swallows_network_errors() -> None:
    def boom(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("down")

    client = DataJudClient(
        base_url="https://api.example",
        api_key="k",
        transport=httpx.MockTransport(boom),
    )
    update = await make_enrich_node(client)(_state(CASE_SP))
    enrichment = update["enrichment"]
    assert enrichment.attempted is True
    assert enrichment.found is False
    assert any("Falha" in note for note in enrichment.notes)
    assert update["traces"][0].status is AgentStatus.FAILED
    assert update["traces"][0].error == "ConnectError: DataJud lookup failed"
