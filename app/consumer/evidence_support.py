"""Evidence page markers and whether an excerpt supports the confirmed facts.

Notice generation indexes every accepted upload as one synthetic document with
one marked section per page, and it cites only excerpts that share the
confirmed case's topic. The marker, the rule that strips it from citations
and the support heuristics live together here.
"""

from __future__ import annotations

import re
from itertools import combinations

from app.consumer.monetary import extract_brl_mentions
from app.consumer.schemas import ConsumerCaseFacts
from app.rag.vector_store import portuguese_lexical_tokens

# Scaffolding injected between evidence pages so the generic chunker keeps
# them in separate sections. It identifies the case, so it must never survive
# into a citation, and it is matched here in upper case because that is the
# only form ``evidence_page_heading`` emits.
_EVIDENCE_PAGE_MARKER_RE = re.compile(
    r"\[?\s*CASO\s+[0-9A-F]{8}\s+EVIDENCIA\s+[0-9A-F]{8}\s+PAGINA\s+\d+\s*\]?",
    re.IGNORECASE,
)

# These words come from the evidence-query scaffolding or generic request
# phrasing. Letting them count as factual overlap would make almost any receipt
# or attachment look supportive merely because it mentions a document, value,
# date or requested solution.
_GENERIC_EVIDENCE_TERMS = frozenset(
    {
        "comprova",
        "comprovacao",
        "comunicacao",
        "comunicacoes",
        "data",
        "datas",
        "documento",
        "evidencia",
        "fato",
        "fatos",
        "prejuizo",
        "providencia",
        "providencias",
        "quero",
        "solicitada",
        "solicitado",
        "solucao",
        "tentativa",
        "valor",
        "valores",
    }
)
_DATE_ANCHOR_RE = re.compile(
    r"\b(?:\d{1,2}[./-]\d{1,2}[./-]\d{2,4}|\d{4}[./-]\d{1,2}[./-]\d{1,2})\b"
)
_LABELED_IDENTIFIER_RE = re.compile(
    r"\b(?:contrato|fatura|ocorrencia|pedido|protocolo)\s*"
    r"(?:n(?:o|ro)?\.?|numero)?\s*[:#-]?\s*([a-z0-9][a-z0-9./-]{3,})",
    re.IGNORECASE,
)
_EMBEDDED_IDENTIFIER_RE = re.compile(
    r"\b(?=[a-z0-9./-]*\d)[a-z0-9][a-z0-9./-]{5,}\b",
    re.IGNORECASE,
)


def evidence_page_heading(case_id: str, evidence_id: str, page: int) -> str:
    """The heading that keeps one uploaded page in its own chunker section.

    It must satisfy SectionAwareChunker.is_heading, which requires ~all
    letters to be upper case. uuid4 hex carries lower-case a-f, so an
    unmodified id made this a heading only ~3% of the time; the rest of the
    time every evidence page fell into one section and chunks packed text
    across two different uploaded files under a single page number. Upper
    case makes detection deterministic, and the notice service still refuses
    to cite any chunk that spans pages. The consumer pipeline now chunks
    evidence per page (SectionAwareChunker's page-preserving mode), which
    keeps pages apart on its own; the marker stays a heading for the default
    chunker.
    """
    return f"CASO {case_id[:8].upper()} EVIDENCIA {evidence_id[:8].upper()} PAGINA {page}"


def clean_chunk_quote(text: str) -> str:
    """Return the document's own words, without the evidence page marker.

    The ``CASO ... EVIDENCIA ... PAGINA n`` marker is scaffolding injected to
    keep evidence pages in separate chunker sections. It names the case, so it
    must never reach an exported notice, and it is removed wherever it appears
    rather than only as a leading section title.
    """
    without_marker = _EVIDENCE_PAGE_MARKER_RE.sub(" ", text)
    lines = without_marker.splitlines()
    first = lines[0].strip() if lines else ""
    if first.startswith("[") and first.endswith("]"):
        lines = lines[1:]
    return " ".join(" ".join(lines).split())[:700]


def evidence_supports_confirmed_facts(text: str, facts: ConsumerCaseFacts) -> bool:
    """Require a retrieved excerpt to support the confirmed case topic."""

    text_tokens = portuguese_lexical_tokens(text)
    if not text_tokens:
        return False
    if _matches_unique_case_identifier(text_tokens, facts):
        return True

    factual_text = " ".join(
        value
        for value in (facts.complaint_summary, facts.desired_resolution)
        if value
    )
    supplier_tokens = set(portuguese_lexical_tokens(facts.bank_name or ""))
    return _has_topical_evidence_overlap(
        _topic_tokens(portuguese_lexical_tokens(factual_text), supplier_tokens),
        _topic_tokens(text_tokens, supplier_tokens),
        hard_anchor_count=_evidence_hard_anchor_count(text, text_tokens, facts),
    )


def _topic_tokens(tokens: list[str], supplier_tokens: set[str]) -> list[str]:
    """Tokens that can carry the case topic: no numbers, scaffolding or supplier."""
    return [
        token
        for token in tokens
        if len(token) >= 3
        and not token.isdigit()
        and token not in _GENERIC_EVIDENCE_TERMS
        and token not in supplier_tokens
    ]


def _fact_texts(facts: ConsumerCaseFacts) -> list[str]:
    return [
        facts.incident_date_or_period or "",
        facts.complaint_summary or "",
        facts.desired_resolution or "",
    ]


def _evidence_hard_anchor_count(
    text: str,
    text_tokens: list[str],
    facts: ConsumerCaseFacts,
) -> int:
    matched: set[str] = set()
    fact_amounts = {
        amount
        for amount in (
            facts.direct_loss_amount,
            facts.improper_payment_amount,
            facts.unsuccessful_scenario_cost_amount,
        )
        if amount is not None and amount > 0
    }
    for fact_text in (facts.complaint_summary, facts.desired_resolution):
        if fact_text:
            fact_amounts.update(item.amount for item in extract_brl_mentions(fact_text))
    if fact_amounts.intersection(item.amount for item in extract_brl_mentions(text)):
        matched.add("amount")

    supplier_tokens = portuguese_lexical_tokens(facts.bank_name or "")
    if supplier_tokens and _contains_token_sequence(text_tokens, supplier_tokens):
        matched.add("supplier")

    date_anchors = {
        match.group(0)
        for fact_text in _fact_texts(facts)
        for match in _DATE_ANCHOR_RE.finditer(fact_text)
    }
    if any(
        _contains_token_sequence(text_tokens, portuguese_lexical_tokens(anchor))
        for anchor in date_anchors
    ):
        matched.add("date")
    return len(matched)


def _matches_unique_case_identifier(
    text_tokens: list[str],
    facts: ConsumerCaseFacts,
) -> bool:
    identifiers = set(facts.prior_protocols)
    identifiers.update(
        match.group(0)
        for protocol in facts.prior_protocols
        for match in _EMBEDDED_IDENTIFIER_RE.finditer(protocol)
    )
    identifiers.update(
        match.group(1)
        for fact_text in _fact_texts(facts)
        for match in _LABELED_IDENTIFIER_RE.finditer(fact_text)
        if any(char.isdigit() for char in match.group(1))
    )
    return any(
        len(compact := "".join(portuguese_lexical_tokens(identifier))) >= 4
        and _contains_compact_identifier(text_tokens, compact)
        for identifier in identifiers
    )


def _has_topical_evidence_overlap(
    fact_tokens: list[str],
    evidence_tokens: list[str],
    *,
    hard_anchor_count: int,
) -> bool:
    """Match a factual phrase, or several shared terms close together in both texts."""

    shared = set(fact_tokens).intersection(evidence_tokens)
    if len(shared) < 2:
        return False

    # Several independent anchors make two topical matches meaningful even if
    # the consumer and the source use a different word order.
    if hard_anchor_count >= 2:
        return True

    fact_bigrams = set(zip(fact_tokens, fact_tokens[1:], strict=False))
    evidence_bigrams = set(zip(evidence_tokens, evidence_tokens[1:], strict=False))
    if fact_bigrams.intersection(evidence_bigrams):
        return True

    required_terms = 2 if hard_anchor_count else 3
    window_width = 4 if hard_anchor_count else 6
    if len(shared) < required_terms:
        return False
    fact_groups = _proximate_term_groups(
        fact_tokens,
        shared,
        group_size=required_terms,
        window_width=window_width,
    )
    if not fact_groups:
        return False
    evidence_groups = _proximate_term_groups(
        evidence_tokens,
        shared,
        group_size=required_terms,
        window_width=window_width,
    )
    return not fact_groups.isdisjoint(evidence_groups)


def _proximate_term_groups(
    tokens: list[str],
    allowed: set[str],
    *,
    group_size: int,
    window_width: int,
) -> set[tuple[str, ...]]:
    groups: set[tuple[str, ...]] = set()
    for index in range(len(tokens)):
        window_terms = sorted(set(tokens[index : index + window_width]).intersection(allowed))
        groups.update(combinations(window_terms, group_size))
    return groups


def _contains_token_sequence(tokens: list[str], sequence: list[str]) -> bool:
    if not sequence or len(sequence) > len(tokens):
        return False
    width = len(sequence)
    return any(
        tokens[index : index + width] == sequence
        for index in range(len(tokens) - width + 1)
    )


def _contains_compact_identifier(tokens: list[str], identifier: str) -> bool:
    """Match an identifier across punctuation splits, never across unrelated text."""

    if len(identifier) < 4:
        return False
    for start in range(len(tokens)):
        candidate = ""
        for end in range(start, len(tokens)):
            candidate += tokens[end]
            if candidate == identifier:
                return True
            if len(candidate) >= len(identifier):
                break
    return False
