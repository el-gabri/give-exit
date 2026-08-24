"""Deterministic Markdown rendering of a LitigationReport.

Pure function of the report model: same report, same Markdown. This is the
canonical export; PDF and DOCX (M7) are derived formats.
"""

from app.schemas.common import ConfidentConclusion
from app.schemas.report import LitigationReport

RISK_LABELS = {"low": "Baixo", "medium": "Medio", "high": "Alto", "critical": "Critico"}
PRIORITY_LABELS = {"urgent": "URGENTE", "high": "Alta", "medium": "Media", "low": "Baixa"}
SECURITY_LABELS = {
    "none": "Nenhum",
    "low": "Baixo",
    "medium": "Medio",
    "high": "Alto",
    "critical": "Critico",
}
SECURITY_ACTION_LABELS = {
    "proceed": "prosseguir",
    "proceed_with_warning": "prosseguir com aviso e mascaramento",
    "human_review": "interromper para revisao humana",
    "block": "bloquear analise automatizada",
}


def _conclusion(c: ConfidentConclusion, indent: str = "") -> list[str]:
    lines = [
        f"{indent}{c.statement}",
        f"{indent}- Confianca: **{c.confidence_pct}%**",
        f"{indent}- Justificativa: {c.reasoning}",
    ]
    for citation in c.citations:
        page = f", p. {citation.page}" if citation.page else ""
        chunk = f", chunk `{citation.chunk_id}`" if citation.chunk_id else ""
        lines.append(f'{indent}- Fonte: "{citation.quote}"{page}{chunk}')
    return lines


def render_markdown(report: LitigationReport) -> str:  # noqa: PLR0912, PLR0915
    md: list[str] = [
        f"# Relatorio de Analise - {report.filename}",
        "",
        f"Documento `{report.doc_id}` · idioma {report.language} · gerado em "
        f"{report.generated_at:%Y-%m-%d %H:%M} UTC",
        "",
        "> **Uso informativo.** Este rascunho automatizado não substitui análise "
        "jurídica individualizada e exige revisão humana antes de qualquer decisão, "
        "envio ou protocolo.",
        "",
        "**Confianca autorrelatada pelos modelos, nao calibrada: "
        f"{round(report.confidence_level * 100)}%**",
        "",
        f"**Integridade de fontes: `{report.evidence_quality.status.value}`**",
        "",
    ]
    if report.evidence_quality.reasons:
        md += [
            "- " + reason
            for reason in report.evidence_quality.reasons
        ]
        md += [
            "- Este controle verifica origem/localização, não nexo semântico, "
            "correção jurídica ou probabilidade de resultado.",
            "",
        ]
    if report.warnings:
        md += ["> **Avisos:** " + " / ".join(report.warnings), ""]

    if security := report.security_assessment:
        risk_label = SECURITY_LABELS.get(
            security.risk_level.value, security.risk_level.value
        )
        action_label = SECURITY_ACTION_LABELS.get(
            security.recommended_action.value,
            security.recommended_action.value,
        )
        scan_status = "completa" if security.scan_complete else "incompleta"
        md += [
            "## Seguranca do Documento",
            "",
            f"- Varredura: **{scan_status}** ({security.scanned_pages} pagina(s))",
            f"- Risco de prompt injection: **{risk_label}**",
            f"- Acao: **{action_label}**",
            f"- Modo: `{security.scan_mode}`",
            "",
        ]
        if security.findings:
            md += ["### Achados", ""]
            for finding in security.findings:
                quote = " ".join(finding.quote.split()).replace("`", "'")
                pages = (
                    f"Paginas {finding.page}-{finding.page_end}"
                    if finding.page_end and finding.page_end != finding.page
                    else f"Pagina {finding.page}"
                )
                md += [
                    f"- {pages} · `{finding.category.value}` · "
                    f"risco **{SECURITY_LABELS[finding.severity.value]}** · "
                    f"detector `{finding.source.value}`",
                    f"  - Trecho: \"{quote}\"",
                    f"  - Motivo: {finding.reasoning}",
                ]
            md += [""]

    md += ["## Resumo Executivo", "", report.executive_summary or "(indisponivel)", ""]

    if report.classification:
        md += ["## Classificacao", ""]
        md += [f"Tipo de acao: **{report.classification.lawsuit_type.value}**", ""]
        md += _conclusion(report.classification.conclusion)
        md += [""]

    if report.parties:
        extraction = report.parties
        md += ["## Partes e Dados do Processo", ""]
        for party in extraction.parties:
            lawyer = f" (adv.: {party.lawyer})" if party.lawyer else ""
            md.append(f"- **{party.role.value}**: {party.name}{lawyer}")
        details = [
            ("Numero do processo", extraction.case_number),
            ("Juizo", extraction.court),
            ("UF", extraction.state),
            ("Juiz(a)", extraction.judge),
            ("Distribuicao", extraction.filing_date),
            (
                "Valor da causa",
                extraction.claim_value.as_written
                if extraction.claim_value
                else None,
            ),
        ]
        md += [f"- {label}: {value}" for label, value in details if value]
        md += [""]

    if report.timeline:
        md += ["## Linha do Tempo", ""]
        for event in report.timeline:
            date = event.date or "data indeterminada"
            md.append(f"- **{date}**: {event.description}")
            if event.citation:
                page = f", p. {event.citation.page}" if event.citation.page else ""
                chunk = (
                    f", chunk `{event.citation.chunk_id}`"
                    if event.citation.chunk_id
                    else ""
                )
                md.append(f'  - Fonte: "{event.citation.quote}"{page}{chunk}')
        md += [""]

    if report.main_claims:
        md += ["## Pedidos e Avaliacao", ""]
        for claim in report.main_claims:
            basis = (
                f" (base legal alegada: {claim.legal_basis})"
                if claim.legal_basis
                else ""
            )
            md += [f"### {claim.claim}{basis}", ""]
            md += _conclusion(claim.assessment)
            md += [""]

    if report.evidence_found:
        md += ["## Provas Identificadas", ""]
        for evidence in report.evidence_found:
            md += _conclusion(evidence)
            md += [""]

    if report.missing_information:
        md += ["## Informacoes Ausentes", ""]
        md += [f"- {item}" for item in report.missing_information]
        md += [""]

    if report.legal_risks:
        risk = report.legal_risks
        overall = RISK_LABELS.get(risk.overall_level.value, risk.overall_level.value)
        md += ["## Riscos Juridicos", "", f"Nivel geral: **{overall}**", ""]
        md += _conclusion(risk.overall)
        md += [""]
        for item in risk.risks:
            level = RISK_LABELS.get(item.level.value, item.level.value)
            md += [f"### {item.title} - risco {level}", ""]
            md += _conclusion(item.conclusion)
            if item.financial_exposure:
                md.append(f"- Exposicao financeira: {item.financial_exposure}")
            md += [""]

    if report.suggested_strategy:
        strategy = report.suggested_strategy
        md += ["## Opcoes Preliminares para Revisao", ""]
        md += _conclusion(strategy.overall_approach)
        md += [""]
        if strategy.defenses:
            md += ["### Linhas de Defesa", ""]
            for defense in strategy.defenses:
                basis = f" (base: {defense.legal_basis})" if defense.legal_basis else ""
                md += [f"**{defense.argument}**{basis}", ""]
                md += _conclusion(defense.assessment)
                md += [""]
        if strategy.next_actions:
            md += ["### Proximas Acoes", ""]
            for action in strategy.next_actions:
                priority = PRIORITY_LABELS.get(action.priority.value, action.priority.value)
                md.append(f"- [{priority}] {action.action} - {action.rationale}")
            md += [""]

    if report.possible_settlement:
        md += ["## Possibilidade de Acordo", ""]
        md += _conclusion(report.possible_settlement)
        md += [""]

    if report.datajud and report.datajud.attempted:
        md += ["## Consulta DataJud (CNJ)", ""]
        if report.datajud.found and report.datajud.info:
            info = report.datajud.info
            md += [
                "Processo localizado na base oficial do CNJ.",
                "",
                f"- Tribunal: {info.tribunal or '-'}",
                f"- Classe: {info.court_class or '-'}",
                f"- Orgao julgador: {info.court_body or '-'}",
                f"- Assuntos: {', '.join(info.subjects) or '-'}",
                f"- Ajuizamento: {info.filing_date or '-'}",
                f"- Movimentacoes: {info.movement_count}",
            ]
            if info.latest_movement:
                md.append(
                    f"- Ultima movimentacao: {info.latest_movement.name} "
                    f"({info.latest_movement.date or 's/ data'})"
                )
        else:
            md += [f"- {note}" for note in report.datajud.notes]
        md += [""]

    md += [
        "## Como a IA Chegou a Estas Conclusoes",
        "",
        report.ai_reasoning or "(indisponivel)",
        "",
    ]

    retrievals = [
        retrieval for trace in report.traces for retrieval in trace.retrievals
    ]
    if retrievals:
        failed_retrievals = [
            retrieval for retrieval in retrievals if retrieval.error is not None
        ]
        included = {
            item.chunk_id
            for retrieval in retrievals
            for item in retrieval.results
            if item.included_in_context
        }
        md += [
            "## Auditoria de Recuperacao",
            "",
            f"- Consultas executadas: {report.metrics.retrieval_queries}",
            f"- Resultados ranqueados: {report.metrics.retrieval_results}",
            f"- Trechos unicos recuperados: {report.metrics.retrieval_unique_chunks}",
            f"- Trechos enviados aos modelos: {len(included)}",
            f"- Duracao de recuperacao: {report.metrics.retrieval_duration_ms:.0f} ms",
            f"- Consultas com falha: {len(failed_retrievals)}",
        ]
        if report.metrics.citation_retrieval_coverage is not None:
            md.append(
                "- Cobertura de citacoes recuperadas: "
                f"{report.metrics.citation_retrieval_coverage:.1%}"
            )
        md += [
            "",
            "Os itens abaixo sao os trechos recuperados que efetivamente entraram "
            "no contexto. O endpoint JSON de auditoria preserva o top-k completo.",
            "",
        ]
        for retrieval in sorted(
            retrievals, key=lambda item: (item.agent, item.query_index)
        ):
            if retrieval.error:
                error = " ".join(retrieval.error.split())
                md += [
                    f"### {retrieval.agent} · consulta {retrieval.query_index + 1}",
                    "",
                    f"`{retrieval.query}`",
                    "",
                    f"- Falha de recuperacao: {error}",
                    "",
                ]
                continue
            selected = [
                hit
                for hit in retrieval.results
                if hit.selected_for_merge and hit.included_in_context
            ]
            if not selected:
                continue
            md += [f"### {retrieval.agent} · consulta {retrieval.query_index + 1}", ""]
            md += [f"`{retrieval.query}`", ""]
            if retrieval.agent_status is not None:
                md += [
                    f"- Status do agente: {retrieval.agent_status.value}",
                    f"- Prompt: {retrieval.prompt_version or '-'}",
                ]
            for hit in sorted(selected, key=lambda result: result.merged_rank or 0):
                section = hit.section or "sem secao"
                md += [
                    f"- Rank original {hit.rank} · score {hit.score:.4f} · "
                    f"chunk `{hit.chunk_id}` · {section} · "
                    f"paginas {hit.page_start}-{hit.page_end}",
                    f"  - SHA-256: `{hit.content_sha256}`",
                ]
                if hit.text_preview:
                    md.append(f'  - Previa: "{hit.text_preview}"')
            md += [""]

    md += [
        "## Metricas da Execucao",
        "",
        f"- Agentes executados: {report.metrics.agents_run}",
        f"- Tokens: {report.metrics.total_tokens}",
        f"- Custo estimado: US$ {report.metrics.total_cost_usd:.4f}",
        f"- Duracao total: {report.metrics.total_duration_ms:.0f} ms",
        f"- Modelos: {', '.join(report.metrics.models_used) or '-'}",
        f"- Prompts: {', '.join(report.metrics.prompt_versions) or '-'}",
        f"- Consultas RAG: {report.metrics.retrieval_queries}",
        f"- Resultados RAG: {report.metrics.retrieval_results}",
        f"- Trechos no contexto: {report.metrics.context_chunks}",
        f"- Duracao RAG: {report.metrics.retrieval_duration_ms:.0f} ms",
        "",
        "---",
        "",
        "*Relatorio gerado por IA como apoio a decisao. Nao substitui a "
        "analise de um advogado.*",
    ]
    return "\n".join(md)
