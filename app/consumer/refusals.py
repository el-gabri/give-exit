"""Why a notice was refused, and what the consumer can change (ADR 0025).

A refusal used to answer HTTP 503 with "Tente novamente" whatever the cause,
and the prompt route then deleted the case and its retrieval audit. For a
deterministic cause a retry repeats the same outcome: the first real long
prompt complaint had no recognised remedy and was cut before its own
citation. A refusal now names its reasons with one hint each and says
whether a retry can help. The service keeps it under the case.
"""

from __future__ import annotations

from dataclasses import dataclass

from app.consumer.legal_policy import LEGAL_GROUND_POLICY_VERSION
from app.consumer.retrieval import truncated_narrative_fields
from app.consumer.schemas import (
    ConsumerCaseFacts,
    NoticeGenerationMode,
    NoticeRefusal,
    NoticeRefusalReason,
)
from app.schemas.trace import RetrievalTrace

_ORDER = list(NoticeRefusalReason)
_HINTS = {
    NoticeRefusalReason.SEMANTIC_RETRIEVAL_UNAVAILABLE: (
        "A busca semântica estava indisponível (o modelo pode estar carregando) e a "
        "busca por palavras sozinha não encontrou apoio suficiente. Tente novamente "
        "em alguns minutos."
    ),
    NoticeRefusalReason.NO_REQUESTED_RESOLUTION: (
        "Diga o que você quer que a empresa faça em uma frase própria, por exemplo: "
        '"Quero a entrega do produto anunciado."'
    ),
    NoticeRefusalReason.NO_RETRIEVAL_AGREEMENT: (
        "Nenhum dispositivo legal foi apontado ao mesmo tempo pela busca semântica e "
        "pela busca por palavras. Descreva o problema de forma direta (o que foi "
        "contratado, o que deu errado e o que a empresa fez) ou cite o dispositivo que "
        'você considera aplicável, por exemplo "art. 35, inciso I, do CDC".'
    ),
    NoticeRefusalReason.NO_ELIGIBLE_GROUND: (
        "Os dispositivos encontrados não podem fundamentar uma notificação de "
        "consumidor a fornecedor, como normas penais, administrativas ou processuais. "
        "Descreva o problema de consumo em si ou cite o dispositivo aplicável."
    ),
    NoticeRefusalReason.GROUNDS_REMOVED_BY_VERIFIER: (
        "A verificação automática concluiu que nenhum dos dispositivos encontrados se "
        "aplica aos fatos relatados. Revise o relato ou cite o dispositivo que você "
        "considera aplicável."
    ),
    NoticeRefusalReason.NO_EVIDENCE_SUPPORT: (
        "Nenhum trecho dos documentos enviados sustenta os fatos confirmados. Envie um "
        "documento que mostre a compra, a cobrança ou a tentativa de solução, ou use no "
        "relato os mesmos termos do documento."
    ),
}
_FIELD_LABELS = {"complaint_summary": "do relato", "desired_resolution": "do pedido"}


@dataclass(frozen=True, slots=True)
class GroundingOutcome:
    """What one generation found, counted at each step of grounding."""

    # Legal chunks that cleared the retrieval-agreement gate.
    supported_legal_chunks: int
    # Grounds selected before the optional verifier, and kept after it.
    selected_grounds: int
    verified_grounds: int
    evidence_required: bool
    evidence_cited: int

    @property
    def lacks_legal_grounds(self) -> bool:
        return self.verified_grounds == 0

    @property
    def lacks_evidence(self) -> bool:
        return self.evidence_required and self.evidence_cited == 0


def notice_refusal(
    *,
    case_id: str,
    mode: NoticeGenerationMode,
    facts: ConsumerCaseFacts,
    legal_traces: list[RetrievalTrace],
    evidence_traces: list[RetrievalTrace],
    outcome: GroundingOutcome,
    corpus_release_id: str,
    corpus_sha256: str | None,
) -> NoticeRefusal | None:
    """The refusal this outcome calls for, or None when the notice may be drafted."""

    if not (outcome.lacks_legal_grounds or outcome.lacks_evidence):
        return None
    reasons = _reasons(facts, legal_traces, evidence_traces, outcome)
    retryable = NoticeRefusalReason.SEMANTIC_RETRIEVAL_UNAVAILABLE in reasons
    traces = [*legal_traces, *evidence_traces]
    return NoticeRefusal(
        case_id=case_id,
        generation_mode=mode,
        message=_message(outcome, retryable=retryable),
        reasons=reasons,
        hints=[_hint(reason, facts) for reason in reasons],
        retryable=retryable,
        corpus_release_id=corpus_release_id,
        corpus_sha256=corpus_sha256,
        legal_ground_policy_version=LEGAL_GROUND_POLICY_VERSION,
        retrievals=traces,
        retrieval_degraded_modes=sorted(
            {trace.degraded_mode for trace in traces if trace.degraded_mode}
        ),
    )


def _reasons(
    facts: ConsumerCaseFacts,
    legal_traces: list[RetrievalTrace],
    evidence_traces: list[RetrievalTrace],
    outcome: GroundingOutcome,
) -> list[NoticeRefusalReason]:
    reasons: set[NoticeRefusalReason] = set()
    if outcome.lacks_legal_grounds:
        reasons.update(_legal_reasons(facts, legal_traces, outcome))
    if outcome.lacks_evidence:
        reasons.add(NoticeRefusalReason.NO_EVIDENCE_SUPPORT)
        if _degraded(evidence_traces):
            reasons.add(NoticeRefusalReason.SEMANTIC_RETRIEVAL_UNAVAILABLE)
    return sorted(reasons, key=_ORDER.index)


def _legal_reasons(
    facts: ConsumerCaseFacts, legal_traces: list[RetrievalTrace], outcome: GroundingOutcome
) -> set[NoticeRefusalReason]:
    reasons: set[NoticeRefusalReason] = set()
    if _degraded(legal_traces):
        reasons.add(NoticeRefusalReason.SEMANTIC_RETRIEVAL_UNAVAILABLE)
    if not (facts.desired_resolution or "").strip():
        reasons.add(NoticeRefusalReason.NO_REQUESTED_RESOLUTION)
    if truncated_narrative_fields(facts):
        reasons.add(NoticeRefusalReason.NARRATIVE_TRUNCATED)
    if outcome.selected_grounds:
        reasons.add(NoticeRefusalReason.GROUNDS_REMOVED_BY_VERIFIER)
    elif outcome.supported_legal_chunks:
        reasons.add(NoticeRefusalReason.NO_ELIGIBLE_GROUND)
    else:
        reasons.add(NoticeRefusalReason.NO_RETRIEVAL_AGREEMENT)
    return reasons


def _degraded(traces: list[RetrievalTrace]) -> bool:
    return any(trace.degraded_mode for trace in traces)


def _message(outcome: GroundingOutcome, *, retryable: bool) -> str:
    missing = [
        label
        for label, lacking in (
            ("fundamentos jurídicos", outcome.lacks_legal_grounds),
            ("trechos dos documentos enviados", outcome.lacks_evidence),
        )
        if lacking
    ]
    message = (
        "Não foi possível gerar o rascunho com segurança porque a recuperação não "
        f"encontrou suporte verificável em {' e '.join(missing)}."
    )
    if retryable:
        return (
            f"{message} A busca semântica estava indisponível; tente novamente em "
            "alguns minutos."
        )
    return (
        f"{message} Gerar de novo com os mesmos dados levará ao mesmo resultado; "
        "veja as sugestões para ajustar o relato."
    )


def _hint(reason: NoticeRefusalReason, facts: ConsumerCaseFacts) -> str:
    if reason is not NoticeRefusalReason.NARRATIVE_TRUNCATED:
        return _HINTS[reason]
    cuts = "; ".join(
        f"{_FIELD_LABELS[name]}, {_thousands(limit)} dos {_thousands(length)} caracteres"
        for name, (length, limit) in truncated_narrative_fields(facts).items()
    )
    return (
        # A long account keeps its start and its last window (ADR 0026); a
        # long request keeps its start.
        f"A busca jurídica não lê o texto inteiro ({cuts}). Resuma o relato ou deixe o "
        "problema principal no começo ou no fim dele, e escreva o pedido em poucas frases."
    )


def _thousands(value: int) -> str:
    return f"{value:,}".replace(",", ".")
