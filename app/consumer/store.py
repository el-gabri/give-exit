"""Ephemeral case repository with possession-token authorization.

Consumer cases contain financial and identity data, so the local MVP keeps
them out of operational telemetry. A production adapter must replace this
repository with authenticated, encrypted persistence.

Because every record lives in this process's heap, each bound below is also a
denial-of-service control: an unbounded case map, message log or idempotency
map is reachable by any caller holding one token.
"""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import secrets
import time
import uuid
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime, timezone

from app.consumer.schemas import (
    ConsumerCaseFacts,
    ConsumerEvidence,
    ConsumerMessage,
    ConsumerNotice,
)
from app.schemas.document import ParsedDocument

DEFAULT_MAX_ACTIVE_CASES = 500
DEFAULT_MAX_MESSAGES_PER_CASE = 200
DEFAULT_MAX_IDEMPOTENCY_KEYS_PER_CASE = 50
DEFAULT_CASE_IDLE_TTL_SECONDS = 86_400.0


class ConsumerCaseNotFoundError(LookupError):
    """Case is absent or the possession token is invalid."""


class ConsumerCaseCapacityError(RuntimeError):
    """The in-process store is full; a new case would evict a live one."""


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
    # The combined evidence document is content-addressed. Retaining its id
    # prevents re-embedding unchanged uploads every time a user regenerates a
    # notice; the lock also collapses accidental double-clicks into one job.
    active_evidence_document_id: str | None = None
    evidence_index_lock: asyncio.Lock = field(default_factory=asyncio.Lock, repr=False)
    # Serializes the whole read-modify-write of a notice (and of an upload) for
    # one case, so two concurrent requests cannot interleave into a record that
    # mixes one generation's facts with another's evidence.
    mutation_lock: asyncio.Lock = field(default_factory=asyncio.Lock, repr=False)
    idempotent_messages: dict[str, str] = field(default_factory=dict)
    created_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    updated_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    last_seen_monotonic: float = field(default_factory=time.monotonic)

    def touch(self) -> None:
        self.updated_at = datetime.now(timezone.utc)
        self.last_seen_monotonic = time.monotonic()

    def remember_assistant_reply(
        self, client_message_id: str, assistant: str, *, max_keys: int
    ) -> None:
        """Record an idempotent reply, retaining only the most recent keys."""
        self.idempotent_messages[client_message_id] = assistant
        while len(self.idempotent_messages) > max_keys:
            self.idempotent_messages.pop(next(iter(self.idempotent_messages)))

    def append_message(self, message: ConsumerMessage, *, max_messages: int) -> None:
        """Append one turn, dropping the oldest so the log cannot grow forever.

        The transcript is a convenience for the consumer, not evidence: facts
        and documents carry their own provenance, so trimming the head of a very
        long conversation never weakens the notice.
        """
        self.messages.append(message)
        if len(self.messages) > max_messages:
            del self.messages[: len(self.messages) - max_messages]


class ConsumerCaseStore:
    """Single-process repository suitable for a local/demo deployment."""

    def __init__(
        self,
        *,
        max_active_cases: int = DEFAULT_MAX_ACTIVE_CASES,
        idle_ttl_seconds: float = DEFAULT_CASE_IDLE_TTL_SECONDS,
        max_messages_per_case: int = DEFAULT_MAX_MESSAGES_PER_CASE,
        max_idempotency_keys_per_case: int = DEFAULT_MAX_IDEMPOTENCY_KEYS_PER_CASE,
        clock: Callable[[], float] | None = None,
    ) -> None:
        if max_active_cases < 1:
            raise ValueError("max_active_cases must be positive")
        if idle_ttl_seconds <= 0:
            raise ValueError("idle_ttl_seconds must be positive")
        self._cases: dict[str, ConsumerCaseRecord] = {}
        self._max_active_cases = max_active_cases
        self._idle_ttl_seconds = idle_ttl_seconds
        self._max_messages_per_case = max_messages_per_case
        self._max_idempotency_keys_per_case = max_idempotency_keys_per_case
        self._clock = clock or time.monotonic

    @property
    def max_messages_per_case(self) -> int:
        return self._max_messages_per_case

    @property
    def max_idempotency_keys_per_case(self) -> int:
        return self._max_idempotency_keys_per_case

    def create(self) -> tuple[ConsumerCaseRecord, str]:
        """Create a case, first reclaiming space from idle ones.

        Expiry runs before the capacity check so ordinary traffic keeps working;
        a genuinely full store refuses the new case rather than evicting another
        consumer's confirmed facts and indexed evidence.
        """
        self.expire_idle_cases()
        if len(self._cases) >= self._max_active_cases:
            raise ConsumerCaseCapacityError(
                "the in-process consumer case store is at capacity"
            )
        case_id = uuid.uuid4().hex
        token = secrets.token_urlsafe(32)
        record = ConsumerCaseRecord(
            case_id=case_id,
            token_digest=_token_digest(case_id, token),
            last_seen_monotonic=self._clock(),
        )
        self._cases[case_id] = record
        return record, token

    def expire_idle_cases(self) -> list[ConsumerCaseRecord]:
        """Remove cases untouched for longer than the TTL and return them.

        The caller owns follow-up cleanup (evidence vectors), because this
        repository deliberately knows nothing about retrieval infrastructure.
        """
        now = self._clock()
        expired = [
            record
            for record in self._cases.values()
            if now - record.last_seen_monotonic >= self._idle_ttl_seconds
        ]
        for record in expired:
            self._cases.pop(record.case_id, None)
        return expired

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
        record.last_seen_monotonic = self._clock()
        return record

    def delete_authorized(self, case_id: str, token: str) -> ConsumerCaseRecord:
        record = self.get_authorized(case_id, token)
        del self._cases[case_id]
        return record


def _token_digest(case_id: str, token: str) -> str:
    return hashlib.sha256(f"{case_id}:{token}".encode()).hexdigest()
