"""Consumer-side complaint intake, legal grounding and settlement scenarios."""

from app.consumer.legal_corpus import (
    CONSUMER_LAW_CORPUS_RELEASE_ID,
    LegalCorpus,
    get_default_legal_corpus,
)
from app.consumer.schemas import (
    ConsumerCaseFacts,
    ConsumerCaseSnapshot,
    ConsumerCaseStatus,
    ConsumerEvidence,
    ConsumerMessage,
    ConsumerNotice,
    EvidenceCitation,
    LegalAuthorityCitation,
    LegalProvision,
    SettlementInputs,
    SettlementScenario,
)
from app.consumer.settlement import SettlementCalculator

__all__ = [
    "CONSUMER_LAW_CORPUS_RELEASE_ID",
    "ConsumerCaseFacts",
    "ConsumerCaseSnapshot",
    "ConsumerCaseStatus",
    "ConsumerEvidence",
    "ConsumerMessage",
    "ConsumerNotice",
    "EvidenceCitation",
    "LegalAuthorityCitation",
    "LegalCorpus",
    "LegalProvision",
    "SettlementCalculator",
    "SettlementInputs",
    "SettlementScenario",
    "get_default_legal_corpus",
]
