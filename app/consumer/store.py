"""Ephemeral case repository with possession-token authorization.

Consumer cases contain financial and identity data, so the local MVP keeps
them out of the append-only business telemetry store. A production adapter
must replace this repository with authenticated, encrypted persistence.
"""

from __future__ import annotations

import hashlib
import hmac
import secrets
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone

from app.consumer.schemas import (
    ConsumerCaseFacts,
    ConsumerEvidence,
    ConsumerMessage,
    ConsumerNotice,
)
from app.schemas.document import ParsedDocument


class ConsumerCaseNotFoundError(LookupError):
    """Case is absent or the possession token is invalid."""


@dataclass
class StoredEvidence:
    public: ConsumerEvidence
    safe_document: ParsedDocument | None = None


@dataclass
class ConsumerCaseRecord:
    case_id: str
    token_digest: str
    facts: ConsumerCaseFacts = field(default_factory=ConsumerCaseFacts)
    messages: list[ConsumerMessage] = field(default_factory=list)
    documents: list[StoredEvidence] = field(default_factory=list)
    facts_confirmed: bool = False
    notice: ConsumerNotice | None = None
    indexed_document_ids: set[str] = field(default_factory=set)
    idempotent_messages: dict[str, str] = field(default_factory=dict)
    created_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    updated_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))

    def touch(self) -> None:
        self.updated_at = datetime.now(timezone.utc)


class ConsumerCaseStore:
    """Single-process repository suitable for a local/demo deployment."""

    def __init__(self) -> None:
        self._cases: dict[str, ConsumerCaseRecord] = {}

    def create(self) -> tuple[ConsumerCaseRecord, str]:
        case_id = uuid.uuid4().hex
        token = secrets.token_urlsafe(32)
        record = ConsumerCaseRecord(
            case_id=case_id,
            token_digest=_token_digest(case_id, token),
        )
        self._cases[case_id] = record
        return record, token

    def indexed_document_ids(self) -> set[str]:
        """Every doc_id still owned by a live case (whatever the token)."""
        ids: set[str] = set()
        for record in self._cases.values():
            ids |= record.indexed_document_ids
        return ids

    def get_authorized(self, case_id: str, token: str) -> ConsumerCaseRecord:
        record = self._cases.get(case_id)
        supplied = _token_digest(case_id, token)
        if record is None or not hmac.compare_digest(record.token_digest, supplied):
            # Same response for unknown cases and invalid credentials prevents
            # case-id enumeration from becoming an authorization side channel.
            raise ConsumerCaseNotFoundError("consumer case not found")
        return record

    def delete_authorized(self, case_id: str, token: str) -> ConsumerCaseRecord:
        record = self.get_authorized(case_id, token)
        del self._cases[case_id]
        return record


def _token_digest(case_id: str, token: str) -> str:
    return hashlib.sha256(f"{case_id}:{token}".encode()).hexdigest()
