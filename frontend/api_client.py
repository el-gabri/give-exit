"""HTTP clients used by the Streamlit frontend.

This module deliberately imports nothing from the backend application so the
UI exercises the same public HTTP contract as any other client.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any, BinaryIO

import requests

EVIDENCE_MEDIA_TYPES = {
    ".pdf": "application/pdf",
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
}

NOTICE_CONNECT_TIMEOUT_SECONDS = 10.0
DEFAULT_NOTICE_READ_TIMEOUT_SECONDS = 1_200.0
NOTICE_READ_TIMEOUT_ENV = "LITIGATION_NOTICE_REQUEST_TIMEOUT_SECONDS"


@dataclass(slots=True)
class ConsumerApiError(RuntimeError):
    """A consumer API request failed with a user-presentable message."""

    message: str
    status_code: int | None = None

    def __str__(self) -> str:
        return self.message


class ConsumerApiClient:
    """Small, token-aware client for the consumer assistance endpoints."""

    def __init__(
        self,
        base_url: str,
        api_key: str | None = None,
        notice_read_timeout_seconds: float | None = None,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.api_key = (api_key or "").strip() or None
        self.notice_read_timeout_seconds = _positive_timeout(
            notice_read_timeout_seconds,
            env_name=NOTICE_READ_TIMEOUT_ENV,
            default=DEFAULT_NOTICE_READ_TIMEOUT_SECONDS,
        )

    def health(self) -> dict[str, Any]:
        return self._json("GET", "/health", timeout=10)

    def create_case(self) -> dict[str, Any]:
        return self._json("POST", "/consumer/cases", timeout=30)

    def get_case(self, case_id: str, token: str) -> dict[str, Any]:
        return self._json(
            "GET",
            f"/consumer/cases/{case_id}",
            token=token,
            timeout=30,
        )

    def send_message(
        self,
        case_id: str,
        token: str,
        text: str,
        client_message_id: str,
    ) -> dict[str, Any]:
        return self._json(
            "POST",
            f"/consumer/cases/{case_id}/messages",
            token=token,
            json={"text": text, "client_message_id": client_message_id},
            timeout=90,
        )

    def update_facts(
        self,
        case_id: str,
        token: str,
        facts: dict[str, Any],
    ) -> dict[str, Any]:
        return self._json(
            "PATCH",
            f"/consumer/cases/{case_id}/facts",
            token=token,
            json=facts,
            timeout=30,
        )

    def upload_document(
        self,
        case_id: str,
        token: str,
        filename: str,
        file_object: BinaryIO,
    ) -> dict[str, Any]:
        media_type = EVIDENCE_MEDIA_TYPES.get(Path(filename).suffix.casefold())
        if media_type is None:
            raise ConsumerApiError("Formato não suportado. Envie um arquivo PDF, PNG ou JPG.")
        return self._json(
            "POST",
            f"/consumer/cases/{case_id}/documents",
            token=token,
            files={"file": (filename, file_object, media_type)},
            timeout=120,
        )

    def generate_notice(self, case_id: str, token: str) -> dict[str, Any]:
        return self._json(
            "POST",
            f"/consumer/cases/{case_id}/notice",
            token=token,
            # Keep connection failures fast while allowing a cold local JUÁ
            # process to load its 4B weights and embed the case evidence.
            timeout=(NOTICE_CONNECT_TIMEOUT_SECONDS, self.notice_read_timeout_seconds),
        )

    def get_notice(self, case_id: str, token: str) -> dict[str, Any]:
        return self._json(
            "GET",
            f"/consumer/cases/{case_id}/notice",
            token=token,
            timeout=60,
        )

    def download_notice(self, case_id: str, token: str, extension: str) -> bytes:
        response = self._request(
            "GET",
            f"/consumer/cases/{case_id}/notice.{extension}",
            token=token,
            timeout=120,
        )
        return response.content

    def delete_case(self, case_id: str, token: str) -> None:
        self._request(
            "DELETE",
            f"/consumer/cases/{case_id}",
            token=token,
            timeout=30,
        )

    def _json(self, method: str, path: str, **kwargs: Any) -> dict[str, Any]:
        response = self._request(method, path, **kwargs)
        try:
            payload = response.json()
        except ValueError as exc:
            raise ConsumerApiError(
                "A API retornou uma resposta inválida. Tente novamente.",
                response.status_code,
            ) from exc
        if not isinstance(payload, dict):
            raise ConsumerApiError(
                "A API retornou um formato inesperado. Tente novamente.",
                response.status_code,
            )
        return payload

    def _request(
        self,
        method: str,
        path: str,
        *,
        token: str | None = None,
        **kwargs: Any,
    ) -> requests.Response:
        headers = dict(kwargs.pop("headers", {}))
        if self.api_key:
            headers["X-API-Key"] = self.api_key
        if token:
            headers["X-Consumer-Case-Token"] = token
        try:
            response = requests.request(
                method,
                f"{self.base_url}{path}",
                headers=headers,
                **kwargs,
            )
            response.raise_for_status()
            return response
        except requests.ConnectTimeout as exc:
            raise ConsumerApiError(
                "Não foi possível iniciar a conexão com a API. Verifique se o backend "
                "está em execução.",
            ) from exc
        except requests.ReadTimeout as exc:
            raise ConsumerApiError(
                "A tela parou de aguardar, mas a API pode continuar gerando o rascunho. "
                "Não clique em gerar novamente: use Atualizar na barra lateral para "
                "verificar se ele ficou pronto.",
            ) from exc
        except requests.Timeout as exc:
            raise ConsumerApiError(
                "A operação excedeu o tempo de espera. Verifique o atendimento antes "
                "de tentar novamente.",
            ) from exc
        except requests.ConnectionError as exc:
            raise ConsumerApiError(
                "Não foi possível conectar à API. Verifique se o backend está em execução.",
            ) from exc
        except requests.RequestException as exc:
            error_response = exc.response
            status_code = error_response.status_code if error_response is not None else None
            detail = _response_detail(error_response)
            if status_code in (401, 403, 404):
                detail = (
                    "Este atendimento não está mais disponível nesta sessão. "
                    "Inicie um novo atendimento."
                )
            elif status_code == 413:
                detail = "O arquivo excede o limite aceito pela API."
            elif status_code == 422 and not detail:
                detail = "Revise os campos informados e tente novamente."
            elif status_code is not None and status_code >= 500 and not detail:
                detail = "A API encontrou um erro interno durante a operação."
            elif not detail:
                detail = "A API não conseguiu concluir a operação. Tente novamente."
            raise ConsumerApiError(detail, status_code) from exc


def _response_detail(response: requests.Response | None) -> str | None:
    if response is None:
        return None
    try:
        body = response.json()
    except ValueError:
        return None
    detail = body.get("detail") if isinstance(body, dict) else None
    if isinstance(detail, str):
        return detail
    if isinstance(detail, list):
        messages = [
            item.get("msg", "") for item in detail if isinstance(item, dict) and item.get("msg")
        ]
        return "; ".join(messages) or None
    if isinstance(detail, dict):
        message = detail.get("message")
        missing = detail.get("missing")
        if isinstance(message, str) and isinstance(missing, list) and missing:
            return f"{message}: {', '.join(str(item) for item in missing)}"
        if isinstance(message, str):
            return message
    return None


def _positive_timeout(
    configured: float | None,
    *,
    env_name: str,
    default: float,
) -> float:
    raw: float | str = configured if configured is not None else os.getenv(env_name, str(default))
    try:
        value = float(raw)
    except (TypeError, ValueError):
        return default
    return value if value > 0 else default
