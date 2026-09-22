"""Markdown rendering of the consumer's extrajudicial notice.

The renderer owns every authoritative field: facts, citations, requests and
the proposed amount. Optional composer prose fills only its five bounded
slots, and untrusted evidence text is escaped before it is embedded.
"""

from __future__ import annotations

import re
from decimal import Decimal

from app.consumer.composer import NoticeProse
from app.consumer.schemas import ConsumerCaseFacts, EvidenceCitation, LegalGround

# Characters through which untrusted excerpt text could inject a link, raw
# HTML or a code span. An uploaded PDF is attacker-controlled input in the
# common fraud scenario, and the notice is rendered as markdown and exported
# from it. Emphasis markers (* and _) are deliberately NOT escaped: they cannot
# create a link, and escaping them turned every masked "CPF 12.***.***/0001-00"
# in real evidence into "12.\*\*\*.\*\*\*/0001-00" in the delivered document.
_MARKDOWN_INLINE_ESCAPE_RE = re.compile(r"([\\`\[\]<>])")


def notice_requests(facts: ConsumerCaseFacts) -> list[str]:
    requests = [facts.desired_resolution or "solução integral do problema relatado"]
    if facts.direct_loss_amount and facts.direct_loss_amount > 0:
        requests.append("restituição do prejuízo direto alegado, após conferência dos comprovantes")
    if facts.prior_protocols:
        requests.append("resposta escrita e fundamentada aos protocolos já registrados")
    requests.append("confirmação escrita das providências adotadas dentro do prazo indicado")
    return requests


def render_notice_markdown(
    *,
    facts: ConsumerCaseFacts,
    evidence: list[EvidenceCitation],
    legal_grounds: list[LegalGround],
    requests: list[str],
    public_proposal: Decimal | None,
    prose: NoticeProse | None = None,
) -> str:
    name = facts.consumer_name or "[PREENCHER NOME DO(A) CONSUMIDOR(A)]"
    supplier = facts.bank_name or "[PREENCHER EMPRESA, FORNECEDOR OU INSTITUIÇÃO]"
    protocols = (
        ", ".join(_single_line(item) for item in facts.prior_protocols)
        or "nenhum protocolo informado"
    )
    lines = [
        "# NOTIFICAÇÃO EXTRAJUDICIAL COM PROPOSTA DE ACORDO",
        "",
        "**À**",
        "",
        f"{_single_line(supplier)}",
        "[PREENCHER ENDEREÇO DA NOTIFICADA]",
        "",
        f"**Notificante:** {_single_line(name)}",
        "[PREENCHER CPF E ENDEREÇO DO(A) NOTIFICANTE]",
        "",
        "## 1. Finalidade",
        "",
        (
            prose.purpose
            if prose is not None
            else "Esta notificação busca solução consensual de uma controvérsia de consumo. "
            "Não se trata de ação judicial nem de reconhecimento definitivo de responsabilidade."
        ),
        "",
        "## 2. Fatos declarados pelo(a) consumidor(a)",
        "",
        facts.complaint_summary or "[PREENCHER RELATO]",
        *(["", prose.facts_framing] if prose is not None else []),
        "",
        f"**Data ou período:** {facts.incident_date_or_period or '[PREENCHER]'}",
        f"**Protocolos anteriores:** {protocols}",
        "",
        "## 3. Documentos de suporte",
        "",
    ]
    # Retrieval identifiers (chunk id, corpus release, content hashes) stay out
    # of the delivered document. They are provenance for the consumer's own
    # review and for /consumer/cases/{id}/notice/retrievals, and every one of
    # them survives unchanged on EvidenceCitation and LegalAuthorityCitation.
    if evidence:
        for item in evidence:
            lines.append(
                f"- **{markdown_inline(item.filename)}, p. {item.page}** — "
                f"{markdown_inline(item.quote)}"
            )
    else:
        lines.append("Nenhum documento anexado a esta geração.")
    lines.extend(["", "## 4. Fundamentos jurídicos", ""])
    if prose is not None:
        lines.extend([prose.legal_transition, ""])
    for ground in legal_grounds:
        authority = ground.authority
        # The official text is quoted verbatim, including its own legislative
        # annotations: trimming a statute to look tidier would make the quote
        # something other than what the source says.
        official_excerpt = _bounded_legal_excerpt(
            authority.official_excerpt or authority.official_text
        )
        unit_suffix = f", {authority.unit_label}" if authority.unit_label else ""
        # ``application_to_facts`` explains how the retrieval policy selected
        # this provision. That is reviewer-facing commentary about our own
        # software; addressed to the supplier it only undercuts the notice.
        lines.append(
            f"- **[{authority.citation_label}{unit_suffix}]({authority.official_url})** — "
            f"{official_excerpt or authority.summary}"
        )
    lines.extend(["", "## 5. Providências solicitadas", ""])
    if prose is not None:
        lines.extend([prose.requests_transition, ""])
    # A confirmed fact may span several lines; a raw newline inside a list item
    # silently drops the bullet for every line after the first.
    lines.extend(f"- {_single_line(request)}" for request in requests)
    lines.extend(["", "## 6. Proposta para composição", ""])
    if public_proposal is None:
        lines.append(
            "Neste momento, propõe-se solução não monetária nos termos dos pedidos acima, "
            "sem atribuição automática de indenização."
        )
    else:
        lines.append(
            f"Para tentativa de composição, propõe-se o valor de **R$ {_brl(public_proposal)}**, "
            "sujeito à conferência dos comprovantes e à revisão humana. O valor é uma âncora "
            "de negociação calculada por cenário, não uma previsão de decisão judicial."
        )
    lines.extend(
        [
            "",
            "## 7. Prazo e encerramento",
            "",
            *([prose.closing, ""] if prose is not None else []),
            "Solicita-se resposta escrita em até "
            f"**{facts.response_deadline_business_days} dias úteis**. "
            "A ausência de acordo não altera direitos, defesas ou prazos legais de qualquer parte.",
        ]
    )
    # Place, date and signature are left as fields to fill rather than
    # generated: the notice is sent on a day the renderer cannot know, and a
    # plausible-looking wrong date on an extrajudicial notice is worse than a
    # blank one.
    lines.extend(
        [
            "",
            "---",
            "",
            "[PREENCHER LOCAL], [PREENCHER DATA].",
            "",
            "___________________________________________",
            "",
            f"{_single_line(name)}",
            "[PREENCHER CPF DO(A) NOTIFICANTE]",
        ]
    )
    # The private reservation value is intentionally unavailable to this
    # renderer, so it cannot leak into an exported notice by accident.
    return "\n".join(lines)


def markdown_inline(text: str) -> str:
    """Escape untrusted text before it is embedded in the notice markdown.

    Evidence excerpts and filenames come from files someone else sent the
    consumer. Left raw, ``[clique aqui](https://...)`` inside a PDF becomes a
    live hyperlink in the notice the consumer reads, exports and forwards.
    """
    return _MARKDOWN_INLINE_ESCAPE_RE.sub(r"\\\1", text)


def _single_line(text: str) -> str:
    """Collapse whitespace so a value can safely become one Markdown line."""
    return " ".join(text.split())


def _brl(value: Decimal) -> str:
    rendered = f"{value:,.2f}"
    return rendered.replace(",", "_").replace(".", ",").replace("_", ".")


def _bounded_legal_excerpt(text: str | None, limit: int = 600) -> str:
    normalized = " ".join((text or "").split())
    if len(normalized) <= limit:
        return normalized
    return normalized[: limit - 1].rsplit(" ", 1)[0] + "…"
