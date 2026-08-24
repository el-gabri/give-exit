"""HTTP clients used by the Streamlit frontend.

This module deliberately imports nothing from the backend application so the
UI exercises the same public HTTP contract as any other client.
"""

from __future__ import annotations

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


@dataclass(slots=True)
class ConsumerApiError(RuntimeError):
    """A consumer API request failed with a user-presentable message."""

    message: str
    status_code: int | None = None

    def __str__(self) -> str:
        return self.message


class ConsumerApiClient:
    """Small, token-aware client for the consumer assistance endpoints."""

    def __init__(self, base_url: str, api_key: str | None = None) -> None:
        self.base_url = base_url.rstrip("/")
        self.api_key = (api_key or "").strip() or None

    def health(self) -> None:
        self._request("GET", "/health", timeout=10)

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
            timeout=120,
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
        except requests.RequestException as exc:
            response = exc.response
            status_code = response.status_code if response is not None else None
            detail = _response_detail(response)
            if status_code in (401, 403, 404):
                detail = (
                    "Este atendimento não está mais disponível nesta sessão. "
                    "Inicie um novo atendimento."
                )
            elif status_code == 413:
                detail = "O arquivo excede o limite aceito pela API."
            elif status_code == 422 and not detail:
                detail = "Revise os campos informados e tente novamente."
            elif not detail:
                detail = "Não foi possível conectar à API. Tente novamente."
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
