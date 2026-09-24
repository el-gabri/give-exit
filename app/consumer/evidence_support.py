"""Evidence quotes and whether an excerpt supports the confirmed facts.

Notice generation indexes every accepted upload as one synthetic document with
one page per uploaded page, and it cites only excerpts that share the
confirmed case's topic. The quote shown for a citation is the passage of the
chunk that best matches the confirmed facts, not simply its first characters.
"""

from __future__ import annotations

import re
from itertools import combinations

from app.consumer.monetary import extract_brl_mentions
from app.consumer.schemas import ConsumerCaseFacts
from app.rag.vector_store import portuguese_lexical_tokens

# Page markers the service once injected between evidence pages. Indexing it
# made every page lexically match any query containing "evidência", "caso" or
# "página", so pages are now kept apart by the page-preserving chunker alone.
# The marker names the case, so any occurrence is still removed from quotes.
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


QUOTE_MAX_CHARS = 700
_WORD_RE = re.compile(r"\S+")
_SENTENCE_END_RE = re.compile(r"[.!?;]\s")


def clean_chunk_quote(text: str) -> str:
    """Return the document's own words, without scaffolding, whitespace-normalized.

    A leading ``[SECTION]`` line is chunker context, and any page marker names
    the case; neither may reach an exported notice.
    """
    without_marker = _EVIDENCE_PAGE_MARKER_RE.sub(" ", text)
    lines = without_marker.splitlines()
    first = lines[0].strip() if lines else ""
    if first.startswith("[") and first.endswith("]"):
        lines = lines[1:]
    return " ".join(" ".join(lines).split())


def supporting_quote(
    text: str, facts: ConsumerCaseFacts, *, max_chars: int = QUOTE_MAX_CHARS
) -> str:
    """The contiguous passage of a chunk that best matches the confirmed facts.

    Quoting the first ``max_chars`` characters left out the supporting fact
    whenever it sat later in a long chunk. The passage is chosen by a sliding
    window over whole words, scored by the case's topic terms and, twice as
    heavily, by its protocols, amounts and dates; it starts at its sentence
    when that fits. With no matching term the chunk's opening is quoted.
    """

    quotable = clean_chunk_quote(text)
    if len(quotable) <= max_chars:
        return quotable
    weights = _fact_term_weights(facts)
    words = [
        (match.start(), match.end(), _word_weight(match.group(0), weights))
        for match in _WORD_RE.finditer(quotable)
    ]
    start = _best_window_start(words, max_chars)
    return _word_bounded(quotable, _sentence_start(quotable, start, max_chars), max_chars)


def _fact_term_weights(facts: ConsumerCaseFacts) -> dict[str, int]:
    supplier = set(portuguese_lexical_tokens(facts.bank_name or ""))
    narrative = " ".join(
        value for value in (facts.complaint_summary, facts.desired_resolution) if value
    )
    weights = dict.fromkeys(_topic_tokens(portuguese_lexical_tokens(narrative), supplier), 1)
    amounts = (facts.direct_loss_amount, facts.improper_payment_amount)
    anchors = " ".join(
        [
            *facts.prior_protocols,
            facts.incident_date_or_period or "",
            *(f"{amount:,.2f}".replace(",", " ") for amount in amounts if amount is not None),
        ]
    )
    for token in portuguese_lexical_tokens(anchors):
        if len(token) >= 3 and token.strip("0"):
            weights[token] = 2
    return weights


def _word_weight(word: str, weights: dict[str, int]) -> int:
    return sum(weights.get(token, 0) for token in portuguese_lexical_tokens(word))


def _best_window_start(words: list[tuple[int, int, int]], max_chars: int) -> int:
    """Offset of the first matching word in the heaviest ``max_chars`` word window.

    The earliest heaviest window ends on its matches; starting at the first of
    them instead keeps every match and puts it at the head of the quote.
    """
    best_index, best_score, score, end = 0, 0, 0, 0
    for index, (start, _, _) in enumerate(words):
        while end < len(words) and words[end][1] - start <= max_chars:
            score += words[end][2]
            end += 1
        if score > best_score:
            best_index, best_score = index, score
        score -= words[index][2]
    if best_score == 0:
        return 0
    return next(start for start, _, weight in words[best_index:] if weight)


def _sentence_start(text: str, start: int, max_chars: int) -> int:
    """Move back to the start of the sentence when that costs a quarter at most."""
    floor = max(0, start - max_chars // 4)
    ends = [match.end() for match in _SENTENCE_END_RE.finditer(text, floor, start)]
    return ends[-1] if ends else (0 if start <= max_chars // 4 else start)


def _word_bounded(text: str, start: int, max_chars: int) -> str:
    """At most ``max_chars`` from ``start``, ending at a sentence end or word break."""
    window = text[start : start + max_chars]
    if start + max_chars >= len(text):
        return window.strip()
    sentence_end = max(window.rfind(". "), window.rfind("; "))
    if sentence_end >= max_chars // 2:
        return window[: sentence_end + 1].strip()
    space = window.rfind(" ")
    return (window[:space] if space > 0 else window).strip()


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
