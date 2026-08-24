"""Deterministic retrieval-query construction for the consumer journey.

The consumer's own account must drive legal retrieval.  Category expansions
add useful statutory vocabulary, but never replace the facts supplied by the
user.  Keeping this logic deterministic also makes retrieval evaluation and
audit replay possible without an LLM call.
"""

from __future__ import annotations

from app.consumer.schemas import ConsumerCaseFacts

MAX_QUERY_CHARS = 2_000

# The intake is intentionally permissive, but a few domains are clearly not
# consumer relationships.  This deterministic gate is conservative: it only
# abstains on an explicit out-of-scope category or on strong negative signals
# without any supplier/product/service signal.  Ambiguous cases remain
# eligible for human review instead of being silently rejected.
_OUT_OF_SCOPE_CATEGORIES = {
    "employment",
    "labor",
    "neighbor_dispute",
    "no_consumer_relationship",
}
_CONSUMER_SIGNALS = (
    "assinatura",
    "banco",
    "cartão",
    "cobrança",
    "compra",
    "comprei",
    "consumidor",
    "contratei",
    "contrato",
    "empresa",
    "entrega",
    "fatura",
    "financiamento",
    "fornecedor",
    "internet",
    "loja",
    "operadora",
    "pagamento",
    "paguei",
    "plano de saúde",
    "produto",
    "seguro",
    "serviço",
)
_NON_CONSUMER_SIGNALS = (
    "benefício trabalhista",
    "demissão",
    "empregador",
    "herança",
    "hora extra",
    "inventário",
    "meu vizinho",
    "salário",
    "vale-transporte",
    "vínculo empregatício",
)

_CATEGORY_EXPANSIONS: dict[str, str] = {
    "unauthorized_charge": (
        "cobrança indevida repetição do indébito pagamento em excesso artigo 42"
    ),
    "fraud": "fraude falha de segurança responsabilidade pelo serviço reparação",
    "account_block": "bloqueio de acesso ou valores continuidade informação reparação",
    "negative_credit_record": (
        "cadastro de consumidores negativação correção de dados cobrança artigo 43"
    ),
    "loan_or_interest": (
        "crédito empréstimo juros custo efetivo total informação contrato consumidor"
    ),
    "service_failure": ("vício de produto ou serviço qualidade adequação reparação artigos 18 20"),
    "product_defect": "vício do produto substituição restituição abatimento artigo 18",
    "non_delivery": "oferta descumprida entrega forçada restituição artigo 35",
    "right_of_withdrawal": (
        "direito de arrependimento contratação fora do estabelecimento artigo 49"
    ),
    "misleading_advertising": "publicidade enganosa ou abusiva oferta informação artigos 36 37 38",
    "abusive_practice": "prática abusiva vantagem manifestamente excessiva artigo 39",
    "abusive_collection": "cobrança de dívida ameaça constrangimento exposição artigo 42",
    "public_utility": "serviço público adequado eficiente seguro contínuo artigo 22",
    "consumer_safety": "proteção à saúde e segurança defeito do produto ou serviço",
    "contract_terms": "contrato de adesão cláusula abusiva interpretação consumidor",
    "over_indebtedness": (
        "superendividamento crédito responsável repactuação conciliação artigos 54-A 104-A"
    ),
    "other": "direitos básicos do consumidor fornecedor produto serviço reparação",
}

_SUBCATEGORY_SIGNALS: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("right_of_withdrawal", ("arrepend", "desisti da compra", "sete dias", "7 dias")),
    ("non_delivery", ("não entreg", "nao entreg", "entrega atrasada")),
    ("product_defect", ("produto com defeito", "produto quebr", "vício do produto")),
    ("misleading_advertising", ("publicidade engan", "propaganda engan", "anúncio engan")),
    ("abusive_collection", ("ameaça na cobrança", "ameaca na cobranca", "cobrança vexatória")),
    ("abusive_practice", ("venda casada", "condicionou a compra", "vantagem excessiva")),
    ("public_utility", ("sem água", "sem energia", "serviço essencial interromp")),
    ("consumer_safety", ("reação alérgica", "risco à saúde", "acidente de consumo")),
    ("contract_terms", ("cláusula abusiva", "contrato ilegível", "contrato de adesão")),
)


def build_legal_queries(facts: ConsumerCaseFacts) -> list[str]:
    """Build bounded, replayable legal queries from confirmed case facts."""

    category = facts.issue_category.value if facts.issue_category is not None else "other"
    category = infer_retrieval_category(category, facts.complaint_summary or "")
    return build_legal_queries_for_case(
        category=category,
        complaint=facts.complaint_summary or "",
        desired_resolution=facts.desired_resolution or "",
    )


def build_legal_queries_for_case(
    *, category: str, complaint: str, desired_resolution: str
) -> list[str]:
    """Build the same production queries for a golden-dataset case."""

    # Reserve space for every signal instead of allowing a long complaint to
    # truncate the requested remedy or the legal/category expansion.
    bounded_complaint = _bounded_component(complaint, 1_050)
    bounded_resolution = _bounded_component(desired_resolution, 500)
    narrative = _join_non_empty(bounded_complaint, bounded_resolution)
    expansion = _bounded_component(
        _CATEGORY_EXPANSIONS.get(category, _CATEGORY_EXPANSIONS["other"]), 320
    )
    queries = [
        _bounded(
            "Situação de consumo relatada: "
            f"{narrative}. Localizar dispositivos legais diretamente aplicáveis."
        ),
        _bounded(f"{narrative}. {expansion}"),
        # Keep one authority-focused query free from the long lay narrative.
        # In the pinned offline benchmark this prevents generic terms in the
        # complaint from drowning out explicit legal anchors such as arts. 18,
        # 35, 42, 43 and 49.  The strategy is versioned by the evaluator, so a
        # future wording change cannot silently move the reported baseline.
        _bounded(expansion),
    ]
    return _unique_non_empty(queries)


def infer_retrieval_category(category: str, complaint: str) -> str:
    """Map a short lay intake taxonomy to a more precise retrieval expansion."""

    normalized = _clean(complaint).casefold()
    for subcategory, signals in _SUBCATEGORY_SIGNALS:
        if any(signal in normalized for signal in signals):
            return subcategory
    return category if category in _CATEGORY_EXPANSIONS else "other"


def is_consumer_scope(*, category: str | None, complaint: str) -> bool:
    """Return whether a case can safely use the consumer-law corpus.

    This is a high-precision abstention rule, not a legal merits classifier.
    It prevents known non-consumer disputes from being dressed in CDC grounds
    while leaving uncertain cases for human review.
    """

    normalized_category = _clean(category).casefold()
    if normalized_category in _OUT_OF_SCOPE_CATEGORIES:
        return False
    if normalized_category in _CATEGORY_EXPANSIONS and normalized_category != "other":
        return True

    normalized_complaint = _clean(complaint).casefold()
    has_consumer_signal = any(signal in normalized_complaint for signal in _CONSUMER_SIGNALS)
    has_non_consumer_signal = any(
        signal in normalized_complaint for signal in _NON_CONSUMER_SIGNALS
    )
    return has_consumer_signal or not has_non_consumer_signal


def build_evidence_queries(facts: ConsumerCaseFacts) -> list[str]:
    """Build evidence lookups tied to the allegation and requested remedy."""

    complaint = _clean(facts.complaint_summary)
    resolution = _clean(facts.desired_resolution)
    return _unique_non_empty(
        [
            _bounded(
                f"Evidência que comprova os fatos, datas, valores e comunicações: {complaint}"
            ),
            _bounded(
                "Documento que comprova o prejuízo, a tentativa de solução e a providência "
                f"solicitada: {resolution}"
            ),
        ]
    )


def _join_non_empty(*values: str | None) -> str:
    return ". ".join(value for value in (_clean(item) for item in values) if value)


def _clean(value: str | None) -> str:
    return " ".join((value or "").split())


def _bounded(value: str) -> str:
    normalized = _clean(value)
    if len(normalized) <= MAX_QUERY_CHARS:
        return normalized
    return normalized[:MAX_QUERY_CHARS].rsplit(" ", 1)[0]


def _bounded_component(value: str | None, max_chars: int) -> str:
    normalized = _clean(value)
    if len(normalized) <= max_chars:
        return normalized
    return normalized[:max_chars].rsplit(" ", 1)[0]


def _unique_non_empty(values: list[str]) -> list[str]:
    seen: set[str] = set()
    result: list[str] = []
    for value in values:
        normalized = _clean(value)
        key = normalized.casefold()
        if not normalized or key in seen:
            continue
        seen.add(key)
        result.append(normalized)
    return result
