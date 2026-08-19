"""DataJud (CNJ public API) client.

POST {base}/api_publica_{alias}/_search with an Elasticsearch-style query
matching numeroProcesso; auth header "Authorization: APIKey <public key>"
(key published at https://datajud-wiki.cnj.jus.br/api-publica/acesso/).

The client is deliberately forgiving: DataJud responses vary per tribunal,
so every field access is defensive - a partial record is still useful.
"""

from typing import Any

import httpx

from app.core.logging import get_logger
from app.enrichment.cnj import normalize_case_number, tribunal_alias
from app.schemas.enrichment import DataJudCaseInfo, DataJudMovement

logger = get_logger(__name__)


class DataJudClient:
    """Looks up official case metadata by CNJ number."""

    def __init__(
        self,
        base_url: str,
        api_key: str,
        timeout_seconds: float = 10.0,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self._base_url = base_url.rstrip("/")
        self._headers = {"Authorization": f"APIKey {api_key}"}
        self._timeout = timeout_seconds
        self._transport = transport  # injectable for tests

    async def lookup(self, case_number: str) -> tuple[str | None, DataJudCaseInfo | None]:
        """Return (tribunal_alias, case info) - info is None when not found."""
        normalized = normalize_case_number(case_number)
        alias = tribunal_alias(case_number)
        if normalized is None or alias is None:
            return alias, None

        url = f"{self._base_url}/api_publica_{alias}/_search"
        payload = {"query": {"match": {"numeroProcesso": normalized}}, "size": 1}
        async with httpx.AsyncClient(
            timeout=self._timeout, transport=self._transport
        ) as client:
            response = await client.post(url, json=payload, headers=self._headers)
            response.raise_for_status()
            body = response.json()

        hits = body.get("hits", {}).get("hits", [])
        if not hits:
            logger.info("datajud_not_found", case=normalized, alias=alias)
            return alias, None
        source = hits[0].get("_source", {})
        info = _parse_source(normalized, source)
        logger.info("datajud_found", case=normalized, alias=alias, classe=info.court_class)
        return alias, info


def _parse_source(case_number: str, source: dict[str, Any]) -> DataJudCaseInfo:
    movements = source.get("movimentos") or []
    latest = None
    if movements:
        latest_raw = max(movements, key=lambda m: m.get("dataHora") or "")
        latest = DataJudMovement(
            code=latest_raw.get("codigo"),
            name=latest_raw.get("nome") or "(sem nome)",
            date=latest_raw.get("dataHora"),
        )
    return DataJudCaseInfo(
        case_number=case_number,
        tribunal=source.get("tribunal"),
        court_class=(source.get("classe") or {}).get("nome"),
        subjects=[
            a.get("nome") for a in (source.get("assuntos") or []) if a.get("nome")
        ],
        court_body=(source.get("orgaoJulgador") or {}).get("nome"),
        degree=source.get("grau"),
        filing_date=source.get("dataAjuizamento"),
        last_update=source.get("dataHoraUltimaAtualizacao"),
        movement_count=len(movements),
        latest_movement=latest,
    )
