"""Consumer-facing Streamlit journey for extrajudicial notices."""

from __future__ import annotations

import html
import re
from collections.abc import Iterable
from typing import Any
from urllib.parse import urlparse
from uuid import uuid4

import streamlit as st

from frontend.api_client import ConsumerApiClient, ConsumerApiError

ISSUE_LABELS = {
    "unauthorized_charge": "Cobrança não reconhecida ou indevida",
    "fraud": "Fraude, golpe ou compra não reconhecida",
    "account_block": "Conta, acesso ou valor bloqueado",
    "negative_credit_record": "Negativação indevida",
    "loan_or_interest": "Empréstimo, financiamento ou juros",
    "service_failure": "Produto ou serviço com problema",
    "over_indebtedness": "Superendividamento",
    "other": "Outro problema de consumo",
}

FACT_LABELS = {
    "consumer_name": "Nome do consumidor",
    "bank_name": "Empresa, fornecedor ou instituição",
    "issue_category": "Tipo de problema",
    "complaint_summary": "Relato do ocorrido",
    "incident_date_or_period": "Data ou período",
    "prior_protocols": "Protocolos anteriores",
    "direct_loss_amount": "Prejuízo material",
    "unsuccessful_scenario_cost_amount": "Custo estimado se não houver acordo",
    "desired_resolution": "Solução pretendida",
    "consumer_relationship": (
        "relação de consumo não identificada; este fluxo não cobre questões "
        "trabalhistas ou entre particulares"
    ),
}

BLOCKED_DOCUMENT_STATUSES = {
    "blocked",
    "rejected",
    "review_required",
    "human_review",
    "failed",
}
OFFICIAL_LEGAL_HOSTS = {
    "planalto.gov.br",
    "www.planalto.gov.br",
    "camara.leg.br",
    "www.camara.leg.br",
    "www2.camara.leg.br",
}
# Inline Markdown links, and the characters that create Markdown structure.
# Both exist so text that reached the UI from an uploaded document is shown
# rather than obeyed.
_MARKDOWN_LINK_RE = re.compile(r"(\[[^\]\n]*\])\(([^)\s]+)\)")
_MARKDOWN_LITERAL_RE = re.compile(r"([\\`*_\[\]])")

CONSUMER_STATE_KEYS = (
    "consumer_case_id",
    "consumer_case_token",
    "consumer_case",
    "consumer_notice",
    "consumer_facts_synced_at",
    "consumer_flash",
    "consumer_upload_generation",
)


def render_consumer_app(api_url: str, api_key: str | None = None) -> None:
    """Render the complete consumer journey."""
    _initialize_state()
    client = ConsumerApiClient(api_url, api_key=api_key)
    connected = _render_sidebar(client, api_url)

    st.title("Assistente para reclamações")
    st.caption(
        "Organize os fatos e as provas para gerar uma notificação extrajudicial "
        "com proposta de acordo — não uma ação judicial."
    )
    st.info(
        "A ferramenta prepara um rascunho com referências à Constituição e ao "
        "Código de Defesa do Consumidor. Revise o documento com um advogado antes "
        "de enviá-lo, sobretudo quando houver prazo, dano urgente ou valor relevante.",
        icon=":material/info:",
    )
    _render_flash()

    if not connected:
        st.error(
            "O atendimento ao consumidor está temporariamente indisponível porque "
            "a API não respondeu. Nenhum dado foi enviado."
        )
        return

    if not _has_case_credentials():
        _render_onboarding(client)
        return

    case = st.session_state.get("consumer_case")
    if not isinstance(case, dict):
        try:
            case = client.get_case(
                st.session_state.consumer_case_id,
                st.session_state.consumer_case_token,
            )
            _store_case(case)
        except ConsumerApiError as exc:
            st.error(str(exc))
            if st.button("Iniciar novo atendimento", icon=":material/restart_alt:"):
                _reset_case_state()
                st.rerun()
            return

    _render_case(client, case)


def _initialize_state() -> None:
    st.session_state.setdefault("consumer_case_id", None)
    st.session_state.setdefault("consumer_case_token", None)
    st.session_state.setdefault("consumer_case", None)
    st.session_state.setdefault("consumer_notice", None)
    st.session_state.setdefault("consumer_facts_synced_at", None)
    st.session_state.setdefault("consumer_flash", None)
    st.session_state.setdefault("consumer_upload_generation", 0)


def _has_case_credentials() -> bool:
    return bool(
        st.session_state.get("consumer_case_id") and st.session_state.get("consumer_case_token")
    )


def _render_sidebar(client: ConsumerApiClient, api_url: str) -> bool:
    with st.sidebar:
        st.title("Give Exit")
        st.caption("Área do consumidor")
        try:
            health = client.health()
            st.success("API conectada", icon=":material/check_circle:")
            if health.get("legal_corpus_ready") is False:
                st.warning(
                    "Base legal ainda não pré-indexada para o modelo configurado.",
                    icon=":material/hourglass_top:",
                )
            connected = True
        except ConsumerApiError:
            st.error("API indisponível", icon=":material/error:")
            st.caption(f"Endereço configurado: `{api_url}`")
            connected = False

        if _has_case_credentials():
            st.divider()
            case_id = st.session_state.consumer_case_id
            st.caption(f"Atendimento `{case_id[:8]}` nesta sessão")
            with st.container(horizontal=True):
                if st.button(
                    "Atualizar",
                    icon=":material/refresh:",
                    disabled=not connected,
                    key="consumer_refresh_case",
                ):
                    try:
                        _store_case(
                            client.get_case(
                                case_id,
                                st.session_state.consumer_case_token,
                            )
                        )
                        _set_flash("success", "Atendimento atualizado.")
                        st.rerun()
                    except ConsumerApiError as exc:
                        st.error(str(exc))
                if st.button(
                    "Apagar",
                    icon=":material/delete:",
                    disabled=not connected,
                    key="consumer_delete_case",
                ):
                    _confirm_delete_case(client)

        st.divider()
        st.caption(
            "O acesso ao rascunho fica restrito a esta sessão por um token. "
            "Este MVP não substitui autenticação, política de retenção e controles "
            "de privacidade para uso em produção."
        )
        st.caption(
            "Conteúdo gerado por IA para apoio informativo. Não substitui "
            "orientação jurídica profissional."
        )
    return connected


@st.dialog("Apagar atendimento")
def _confirm_delete_case(client: ConsumerApiClient) -> None:
    st.write(
        "Isso remove o atendimento e os documentos indexados pela API. "
        "Baixe o rascunho antes de continuar, se quiser guardá-lo."
    )
    confirm = st.checkbox(
        "Confirmo que quero apagar este atendimento",
        key="consumer_delete_confirm",
    )
    if st.button(
        "Apagar definitivamente",
        type="primary",
        icon=":material/delete_forever:",
        disabled=not confirm,
        width="stretch",
    ):
        try:
            client.delete_case(
                st.session_state.consumer_case_id,
                st.session_state.consumer_case_token,
            )
        except ConsumerApiError as exc:
            st.error(str(exc))
            return
        _reset_case_state()
        _set_flash("success", "Atendimento apagado.")
        st.rerun()


def _render_onboarding(client: ConsumerApiClient) -> None:
    with st.container(border=True):
        st.subheader("Como funciona")
        st.markdown(
            "1. Conte o problema em suas palavras.\n"
            "2. Confirme os fatos organizados pelo assistente.\n"
            "3. Envie PDFs ou imagens que sustentem o relato.\n"
            "4. Revise e baixe a notificação e a memória de cálculo."
        )
        st.caption(
            "Evite enviar senhas, códigos de autenticação ou dados que não sejam "
            "necessários para a reclamação."
        )

    understood = st.checkbox(
        "Entendo que este é um rascunho informativo e que documentos podem conter dados pessoais",
        key="consumer_onboarding_ack",
    )
    if st.button(
        "Iniciar atendimento",
        type="primary",
        icon=":material/chat:",
        disabled=not understood,
        width="stretch",
    ):
        try:
            with st.status("Abrindo atendimento…", expanded=True) as status:
                payload = client.create_case()
                case = _case_from_payload(payload)
                case_id = payload.get("case_id") or case.get("case_id")
                token = payload.get("case_token")
                if not case_id or not token:
                    raise ConsumerApiError("A API não retornou as credenciais do atendimento.")
                st.session_state.consumer_case_id = str(case_id)
                st.session_state.consumer_case_token = str(token)
                _store_case(case)
                _merge_assistant_message(case, payload.get("assistant_message"))
                status.update(
                    label="Atendimento iniciado",
                    state="complete",
                    expanded=False,
                )
            st.rerun()
        except ConsumerApiError as exc:
            st.error(str(exc))


def _render_case(client: ConsumerApiClient, case: dict[str, Any]) -> None:
    case_id = st.session_state.consumer_case_id
    token = st.session_state.consumer_case_token

    st.caption(
        f"Atendimento `{case_id[:8]}` · acesso mantido nesta sessão do navegador; "
        "caso armazenado temporariamente pela API"
    )
    _render_conversation_and_summary(case)

    st.divider()
    _render_facts_form(client, case_id, token, case)

    st.divider()
    _render_evidence_section(client, case_id, token, case)

    st.divider()
    _render_generation_section(client, case_id, token, case)

    notice = st.session_state.get("consumer_notice")
    if not isinstance(notice, dict) and case.get("notice_available"):
        try:
            notice = client.get_notice(case_id, token)
            st.session_state.consumer_notice = _notice_from_payload(notice)
        except ConsumerApiError as exc:
            st.warning(f"Não foi possível recuperar o rascunho já gerado: {exc}")
            notice = None
    if isinstance(notice, dict):
        st.divider()
        _render_notice(client, case_id, token, notice)

    prompt = st.chat_input(
        "Conte o que aconteceu ou responda à pergunta do assistente",
        key="consumer_chat_input",
        max_chars=8_000,
        submit_mode="disable",
    )
    if prompt and prompt.strip():
        try:
            with st.status("Organizando o relato…", expanded=True) as status:
                payload = client.send_message(
                    case_id,
                    token,
                    prompt.strip(),
                    client_message_id=uuid4().hex,
                )
                updated_case = _case_from_payload(payload)
                _merge_assistant_message(
                    updated_case,
                    payload.get("assistant_message"),
                )
                _store_case(updated_case)
                st.session_state.consumer_notice = None
                status.update(
                    label="Relato atualizado",
                    state="complete",
                    expanded=False,
                )
            st.rerun()
        except ConsumerApiError as exc:
            st.error(str(exc))


def _render_conversation_and_summary(case: dict[str, Any]) -> None:
    chat_column, summary_column = st.columns([3, 2], gap="large")
    with chat_column:
        st.subheader("Conversa")
        messages = case.get("messages") or []
        with st.container(border=True, height=360):
            if not messages:
                with st.chat_message("assistant"):
                    st.write(
                        "Conte o que aconteceu, com qual empresa ou fornecedor, "
                        "e qual solução você procura."
                    )
            for message in messages:
                if not isinstance(message, dict):
                    continue
                role = str(message.get("role") or "assistant").lower()
                streamlit_role = "user" if role in {"user", "consumer"} else "assistant"
                content = message.get("content") or message.get("text") or ""
                with st.chat_message(streamlit_role):
                    st.text(str(content))

    with summary_column:
        st.subheader("Resumo do caso")
        with st.container(border=True, height=360):
            facts = case.get("facts") or {}
            supplier = facts.get("bank_name") or "Não informado"
            category = ISSUE_LABELS.get(
                str(facts.get("issue_category")),
                facts.get("issue_category") or "Não definido",
            )
            st.markdown(f"**Empresa ou fornecedor:** {supplier}")
            st.markdown(f"**Problema:** {category}")
            if summary := facts.get("complaint_summary"):
                st.caption(str(summary)[:500])

            documents = case.get("documents") or []
            with st.container(horizontal=True):
                st.metric("Documentos", len(documents), border=True)
                st.metric(
                    "Fatos confirmados",
                    "Sim" if case.get("facts_confirmed") else "Não",
                    border=True,
                )

            missing = case.get("missing_fields") or []
            if missing:
                labels = [FACT_LABELS.get(str(item), str(item)) for item in missing]
                st.warning("Ainda falta: " + ", ".join(labels))
            elif case.get("ready_for_notice"):
                st.success("Fatos mínimos preenchidos.")


def _render_facts_form(
    client: ConsumerApiClient,
    case_id: str,
    token: str,
    case: dict[str, Any],
) -> None:
    st.subheader("Confirme os fatos")
    st.caption(
        "Priorize o resumo do ocorrido e a solução esperada. O assistente usa "
        "os documentos anexados para sustentar o rascunho."
    )
    facts = case.get("facts") or {}
    _sync_fact_widgets(case, facts)

    with st.form("consumer_facts_form"):
        name_column, supplier_column = st.columns(2)
        consumer_name = name_column.text_input(
            "Seu nome completo",
            key="consumer_fact_consumer_name",
        )
        supplier_name = supplier_column.text_input(
            "Empresa, fornecedor ou instituição",
            key="consumer_fact_bank_name",
        )
        complaint_summary = st.text_area(
            "Resumo do ocorrido",
            key="consumer_fact_complaint_summary",
            height=160,
            help="Conte em suas palavras o que aconteceu, em ordem aproximada.",
        )
        desired_resolution = st.text_area(
            "Qual solução você espera?",
            key="consumer_fact_desired_resolution",
            height=110,
            help="Por exemplo: estorno, troca, cancelamento, reparo ou regularização.",
        )

        category_column, date_column = st.columns(2)
        issue_category = category_column.selectbox(
            "Tipo principal do problema",
            options=list(ISSUE_LABELS),
            format_func=lambda value: ISSUE_LABELS.get(value, value),
            key="consumer_fact_issue_category",
        )
        incident_period = date_column.text_input(
            "Data ou período do ocorrido",
            key="consumer_fact_incident_date_or_period",
            help="Pode ser uma data aproximada.",
        )

        with st.expander("Detalhes adicionais (opcional)"):
            st.caption(
                "Preencha apenas o que souber. Esses dados ajudam no cálculo e "
                "na organização da notificação."
            )
            prior_protocols = st.text_area(
                "Protocolos e reclamações anteriores (um por linha)",
                key="consumer_fact_prior_protocols",
                height=80,
            )
            amount_column, paid_column = st.columns(2)
            direct_loss = amount_column.number_input(
                "Prejuízo direto (R$)",
                min_value=0.0,
                step=100.0,
                format="%.2f",
                key="consumer_fact_direct_loss_amount",
            )
            improper_payment = paid_column.number_input(
                "Valor pago em cobrança contestada (R$)",
                min_value=0.0,
                step=100.0,
                format="%.2f",
                key="consumer_fact_improper_payment_amount",
                help=(
                    "Informe somente a parte do prejuízo efetivamente paga "
                    "em uma cobrança contestada."
                ),
            )
            cost_column, deadline_column = st.columns(2)
            unsuccessful_cost = cost_column.number_input(
                "Custo estimado se não houver acordo (R$)",
                min_value=0.0,
                step=100.0,
                format="%.2f",
                key="consumer_fact_unsuccessful_scenario_cost_amount",
                help=(
                    "Informe somente custos estimados por você. O sistema não "
                    "presume custas, honorários ou duração de um processo."
                ),
            )
            response_deadline = deadline_column.number_input(
                "Prazo de resposta (dias úteis)",
                min_value=1,
                max_value=30,
                step=1,
                key="consumer_fact_response_deadline_business_days",
            )
            article_42_requested = st.checkbox(
                "Avaliar, de forma condicional, a devolução em dobro do valor pago",
                key="consumer_fact_article_42_double_repayment_requested",
                help=(
                    "A devolução em dobro depende dos requisitos legais e não "
                    "é aplicada automaticamente."
                ),
            )

        confirmed = st.checkbox(
            "Confirmo que este resumo corresponde ao meu relato e não contém informação inventada",
            key="consumer_fact_confirmed",
        )
        submitted = st.form_submit_button(
            "Salvar e confirmar fatos",
            type="primary",
            icon=":material/fact_check:",
            width="stretch",
        )

    if not submitted:
        return
    if not supplier_name.strip() or not complaint_summary.strip() or not desired_resolution.strip():
        st.error("Informe a empresa ou fornecedor, o resumo do ocorrido e a solução esperada.")
        return

    stored_loss = float(facts.get("direct_loss_amount") or 0.0)
    retained_loss_reference = (
        facts.get("direct_loss_reference_id")
        if abs(float(direct_loss) - stored_loss) < 0.005
        else None
    )
    payload = {
        "consumer_name": consumer_name.strip() or None,
        # Legacy API key retained for compatibility; semantically this is the supplier.
        "bank_name": supplier_name.strip(),
        "issue_category": issue_category,
        "complaint_summary": complaint_summary.strip(),
        "incident_date_or_period": incident_period.strip() or None,
        "prior_protocols": _split_protocols(prior_protocols),
        "direct_loss_amount": float(direct_loss) if direct_loss else None,
        "direct_loss_reference_id": retained_loss_reference,
        "improper_payment_amount": (float(improper_payment) if improper_payment else None),
        "article_42_double_repayment_requested": bool(article_42_requested),
        "unsuccessful_scenario_cost_amount": (
            float(unsuccessful_cost) if unsuccessful_cost else None
        ),
        "desired_resolution": desired_resolution.strip(),
        "response_deadline_business_days": int(response_deadline),
        "facts_confirmed": bool(confirmed),
    }
    try:
        updated = client.update_facts(case_id, token, payload)
        _store_case(_case_from_payload(updated))
        st.session_state.consumer_notice = None
        _set_flash("success", "Fatos salvos e atualizados.")
        st.rerun()
    except ConsumerApiError as exc:
        st.error(str(exc))


def _sync_fact_widgets(case: dict[str, Any], facts: dict[str, Any]) -> None:
    version = case.get("updated_at") or repr(sorted(facts.items()))
    required_widget_keys = {
        "consumer_fact_consumer_name",
        "consumer_fact_bank_name",
        "consumer_fact_issue_category",
        "consumer_fact_complaint_summary",
        "consumer_fact_incident_date_or_period",
        "consumer_fact_prior_protocols",
        "consumer_fact_direct_loss_amount",
        "consumer_fact_improper_payment_amount",
        "consumer_fact_article_42_double_repayment_requested",
        "consumer_fact_unsuccessful_scenario_cost_amount",
        "consumer_fact_desired_resolution",
        "consumer_fact_response_deadline_business_days",
        "consumer_fact_confirmed",
    }
    if st.session_state.get(
        "consumer_facts_synced_at"
    ) == version and required_widget_keys.issubset(st.session_state):
        return
    current_category = str(facts.get("issue_category") or "other")
    if current_category not in ISSUE_LABELS:
        current_category = "other"
    values = {
        "consumer_fact_consumer_name": facts.get("consumer_name") or "",
        "consumer_fact_bank_name": facts.get("bank_name") or "",
        "consumer_fact_issue_category": current_category,
        "consumer_fact_complaint_summary": facts.get("complaint_summary") or "",
        "consumer_fact_incident_date_or_period": (facts.get("incident_date_or_period") or ""),
        "consumer_fact_prior_protocols": "\n".join(
            str(item) for item in (facts.get("prior_protocols") or [])
        ),
        "consumer_fact_direct_loss_amount": float(facts.get("direct_loss_amount") or 0.0),
        "consumer_fact_improper_payment_amount": float(facts.get("improper_payment_amount") or 0.0),
        "consumer_fact_article_42_double_repayment_requested": bool(
            facts.get("article_42_double_repayment_requested")
        ),
        "consumer_fact_unsuccessful_scenario_cost_amount": float(
            facts.get("unsuccessful_scenario_cost_amount") or 0.0
        ),
        "consumer_fact_desired_resolution": facts.get("desired_resolution") or "",
        "consumer_fact_response_deadline_business_days": int(
            facts.get("response_deadline_business_days") or 10
        ),
        "consumer_fact_confirmed": bool(case.get("facts_confirmed")),
    }
    for key, value in values.items():
        st.session_state[key] = value
    st.session_state.consumer_facts_synced_at = version


def _render_evidence_section(
    client: ConsumerApiClient,
    case_id: str,
    token: str,
    case: dict[str, Any],
) -> None:
    st.subheader("Documentos e evidências")
    recommended = case.get("recommended_documents") or []
    if recommended:
        with st.expander("Documentos recomendados", expanded=not case.get("documents")):
            for item in recommended:
                label = item.get("label") if isinstance(item, dict) else item
                st.markdown(f"- {label}")

    documents = case.get("documents") or []
    for document in documents:
        if isinstance(document, dict):
            _render_document(document)
    _render_monetary_candidate_confirmation(client, case_id, token, case)

    upload_key = f"consumer_evidence_{st.session_state.consumer_upload_generation}"
    with st.form("consumer_evidence_upload"):
        uploads = st.file_uploader(
            "Envie comprovantes em PDF, PNG ou JPG",
            type=["pdf", "png", "jpg", "jpeg"],
            accept_multiple_files=True,
            max_upload_size=20,
            key=upload_key,
            help=(
                "Exemplos: nota fiscal, contrato, fatura, captura de tela, "
                "conversa, protocolo ou resposta da empresa."
            ),
        )
        st.caption(
            "Cada arquivo passa por varredura de instruções maliciosas antes de "
            "ser usado como evidência."
        )
        submitted = st.form_submit_button(
            "Analisar documentos",
            icon=":material/upload_file:",
            width="stretch",
        )

    if not submitted:
        return
    if not uploads:
        st.warning("Selecione ao menos um arquivo PDF, PNG ou JPG.")
        return

    failures: list[str] = []
    latest_case = case
    with st.status("Analisando documentos…", expanded=True) as status:
        for upload in uploads:
            status.write(f"Verificando `{upload.name}`")
            try:
                upload.seek(0)
                response = client.upload_document(
                    case_id,
                    token,
                    upload.name,
                    upload,
                )
                latest_case = _case_from_payload(response)
            except ConsumerApiError as exc:
                failures.append(f"{upload.name}: {exc}")
        if failures:
            status.update(
                label="Análise concluída com pendências",
                state="error",
                expanded=True,
            )
        else:
            status.update(
                label="Documentos analisados",
                state="complete",
                expanded=False,
            )

    _store_case(latest_case)
    st.session_state.consumer_notice = None
    st.session_state.consumer_upload_generation += 1
    if failures:
        _set_flash("warning", " | ".join(failures))
    else:
        _set_flash("success", "Documentos analisados e vinculados ao atendimento.")
    st.rerun()


def _render_document(document: dict[str, Any]) -> None:
    status = str(document.get("status") or "processed")
    assessment = document.get("security_assessment") or {}
    risk = assessment.get("risk_level") or document.get("risk_level") or "unknown"
    filename = document.get("filename") or document.get("name") or "Documento"
    pages = document.get("pages") or document.get("page_count")
    with st.container(border=True):
        st.markdown(f"**{_markdown_literal(filename)}**")
        details = [f"status: {status}", f"segurança: {risk}"]
        if pages:
            details.append(f"{pages} página(s)")
        st.caption(" · ".join(details))
        warnings = document.get("warnings") or []
        if isinstance(warnings, str):
            warnings = [warnings]
        for warning in warnings:
            st.warning(str(warning))
        if status in BLOCKED_DOCUMENT_STATUSES or risk in {"high", "critical"}:
            st.error(
                "Este arquivo não será usado automaticamente no rascunho. "
                "Revise o alerta de segurança."
            )


def _render_monetary_candidate_confirmation(
    client: ConsumerApiClient,
    case_id: str,
    token: str,
    case: dict[str, Any],
) -> None:
    candidates: dict[str, dict[str, Any]] = {}
    for document in case.get("documents") or []:
        if not isinstance(document, dict):
            continue
        for reference in document.get("monetary_references") or []:
            if not isinstance(reference, dict):
                continue
            reference_id = str(reference.get("reference_id") or "")
            if not reference_id:
                continue
            candidates[reference_id] = {
                **reference,
                "filename": document.get("filename") or "Documento",
            }
    if not candidates:
        return

    facts = case.get("facts") or {}
    current_reference = str(facts.get("direct_loss_reference_id") or "")
    options = ["", *candidates]
    default_index = options.index(current_reference) if current_reference in options else 0

    with st.expander(
        "Valores encontrados nos documentos",
        expanded=not bool(facts.get("direct_loss_amount")),
    ):
        st.caption(
            "O assistente encontrou valores explícitos. Selecione apenas o que realmente "
            "corresponde ao seu prejuízo; isso não define a compensação ou indenização."
        )
        with st.form("consumer_monetary_candidate_form"):
            selected = st.selectbox(
                "Valor documentado",
                options=options,
                index=default_index,
                format_func=lambda reference_id: (
                    "Não selecionar agora"
                    if not reference_id
                    else _monetary_candidate_label(candidates[reference_id])
                ),
            )
            confirmed = st.checkbox(
                "Confirmo que o valor selecionado corresponde ao meu prejuízo direto"
            )
            submitted = st.form_submit_button(
                "Usar no cálculo",
                icon=":material/check:",
                width="stretch",
            )

    if not submitted:
        return
    if not selected:
        st.warning("Selecione um valor documentado.")
        return
    if not confirmed:
        st.warning("Confirme que o valor corresponde ao prejuízo direto.")
        return

    candidate = candidates[selected]
    try:
        updated = client.update_facts(
            case_id,
            token,
            {
                "direct_loss_amount": float(candidate["amount"]),
                "direct_loss_reference_id": selected,
            },
        )
    except ConsumerApiError as exc:
        st.error(str(exc))
        return
    _store_case(_case_from_payload(updated))
    _set_flash(
        "success",
        "Valor documentado registrado. Revise e confirme os fatos antes de gerar.",
    )
    st.rerun()


def _monetary_candidate_label(candidate: dict[str, Any]) -> str:
    amount = _format_brl(float(candidate.get("amount") or 0.0))
    filename = str(candidate.get("filename") or "Documento")
    page = int(candidate.get("page") or 1)
    quote = " ".join(str(candidate.get("quote") or "").split())
    suffix = f" — {quote[:90]}" if quote else ""
    return f"{amount} · {filename}, p. {page}{suffix}"


def _render_generation_section(
    client: ConsumerApiClient,
    case_id: str,
    token: str,
    case: dict[str, Any],
) -> None:
    st.subheader("Gerar notificação extrajudicial")
    documents = [item for item in (case.get("documents") or []) if isinstance(item, dict)]
    usable_documents = [
        item
        for item in documents
        if str(item.get("status") or "processed") not in BLOCKED_DOCUMENT_STATUSES
    ]
    missing = case.get("missing_fields") or []
    ready = bool(case.get("ready_for_notice"))
    confirmed = bool(case.get("facts_confirmed"))

    if missing:
        labels = [FACT_LABELS.get(str(item), str(item)) for item in missing]
        st.warning("Complete os campos obrigatórios: " + ", ".join(labels))
    if not confirmed:
        st.warning("Salve o formulário marcando a confirmação dos fatos.")

    if not usable_documents:
        st.warning(
            "É necessário ao menos um documento aceito pela varredura de segurança. "
            "O sistema não gera uma notificação que apresente alegações sem suporte "
            "documental como fatos comprovados."
        )

    can_generate = ready and confirmed and bool(usable_documents)
    button_label = "Gerar nova versão" if case.get("notice_available") else "Gerar rascunho"
    if st.button(
        button_label,
        type="primary",
        icon=":material/draft:",
        disabled=not can_generate,
        width="stretch",
        key="consumer_generate_notice",
    ):
        with st.status(
            "Recuperando base legal e compondo o documento…",
            expanded=True,
        ) as status:
            try:
                notice = client.generate_notice(case_id, token)
                st.session_state.consumer_notice = _notice_from_payload(notice)
                case["notice_available"] = True
                _store_case(case)
                status.update(
                    label="Rascunho gerado",
                    state="complete",
                    expanded=False,
                )
            except ConsumerApiError as exc:
                status.update(
                    label="Não foi possível gerar o rascunho",
                    state="error",
                    expanded=True,
                )
                st.error(str(exc))
                return
        _set_flash(
            "success",
            "Rascunho gerado. Revise fatos, valores, evidências e base legal.",
        )
        st.rerun()


def _render_notice(
    client: ConsumerApiClient,
    case_id: str,
    token: str,
    notice: dict[str, Any],
) -> None:
    st.subheader("Rascunho para revisão")
    for warning in _as_text_list(notice.get("warnings")):
        st.warning(warning)

    notice_tab, legal_tab, evidence_tab, scenario_tab, audit_tab = st.tabs(
        ["Notificação", "Base legal", "Evidências", "Cenário", "Auditoria"]
    )
    with notice_tab:
        full_text = str(notice.get("full_text") or "Rascunho indisponível.")
        st.markdown(_safe_generated_markdown(full_text))

    with legal_tab:
        _render_legal_grounds(notice.get("legal_grounds") or [])

    with evidence_tab:
        _render_evidence_references(notice.get("evidence_references") or [])

    with scenario_tab:
        _render_settlement_scenario(
            notice.get("settlement") or notice.get("settlement_scenario") or {}
        )

    with audit_tab:
        _render_consumer_audit(notice)

    st.caption(
        "Os arquivos são buscados somente quando você clica em baixar; o token "
        "do atendimento não é colocado na URL."
    )
    short_id = case_id[:8]
    markdown_column, pdf_column, docx_column = st.columns(3)
    markdown_column.download_button(
        "Baixar Markdown",
        data=lambda: client.download_notice(case_id, token, "md"),
        file_name=f"notificacao_{short_id}.md",
        mime="text/markdown",
        icon=":material/download:",
        width="stretch",
        on_click="ignore",
        key=f"consumer_download_md_{short_id}",
    )
    pdf_column.download_button(
        "Baixar PDF",
        data=lambda: client.download_notice(case_id, token, "pdf"),
        file_name=f"notificacao_{short_id}.pdf",
        mime="application/pdf",
        icon=":material/picture_as_pdf:",
        width="stretch",
        on_click="ignore",
        key=f"consumer_download_pdf_{short_id}",
    )
    docx_column.download_button(
        "Baixar DOCX",
        data=lambda: client.download_notice(case_id, token, "docx"),
        file_name=f"notificacao_{short_id}.docx",
        mime="application/vnd.openxmlformats-officedocument.wordprocessingml.document",
        icon=":material/description:",
        width="stretch",
        on_click="ignore",
        key=f"consumer_download_docx_{short_id}",
    )


def _render_legal_grounds(grounds: Iterable[Any]) -> None:
    rendered = False
    for ground in grounds:
        if not isinstance(ground, dict):
            continue
        rendered = True
        authority = ground.get("authority")
        if not isinstance(authority, dict):
            authority = ground
        source = authority.get("source_name") or authority.get("source") or "Base legal"
        article = authority.get("article") or authority.get("provision_id") or ""
        with st.container(border=True):
            st.markdown(f"**{source} · {article}**")
            if summary := authority.get("summary"):
                st.write(summary)
            application = ground.get("application_to_facts") or ground.get("application")
            if application:
                st.markdown("**Aplicação ao relato**")
                st.write(application)
            details = []
            if authority.get("retrieval_rank") is not None:
                details.append(f"rank {authority['retrieval_rank']}")
            if authority.get("retrieval_score") is not None:
                details.append(f"score {float(authority['retrieval_score']):.4f}")
            if authority.get("chunk_id"):
                details.append(f"chunk {authority['chunk_id']}")
            if details:
                st.caption(" · ".join(details))
            official_url = str(authority.get("official_url") or "")
            if _is_official_legal_url(official_url):
                st.link_button(
                    "Abrir fonte oficial",
                    official_url,
                    icon=":material/open_in_new:",
                )
    if not rendered:
        st.info("Nenhum fundamento legal foi vinculado ao rascunho.")
    else:
        st.caption(
            "Os textos exibidos são resumos referenciais. Confira a redação vigente "
            "nos links oficiais antes do envio."
        )


def _render_evidence_references(references: Iterable[Any]) -> None:
    rendered = False
    for reference in references:
        if not isinstance(reference, dict):
            continue
        rendered = True
        filename = reference.get("filename") or reference.get("source") or "Documento"
        page = reference.get("page") or reference.get("page_start")
        page_label = f" · página {page}" if page is not None else ""
        with st.container(border=True):
            st.markdown(f"**{_markdown_literal(filename)}{page_label}**")
            quote = reference.get("quote") or reference.get("text_preview")
            if quote:
                st.code(str(quote), language=None, wrap_lines=True)
            details = []
            if reference.get("chunk_id"):
                details.append(f"chunk {reference['chunk_id']}")
            if reference.get("content_sha256"):
                details.append(f"SHA-256 {reference['content_sha256']}")
            if details:
                st.caption(" · ".join(details))
    if not rendered:
        st.info(
            "O rascunho não contém referência documental. Não trate o relato como "
            "fato comprovado até reunir evidências."
        )


def _render_settlement_scenario(scenario: dict[str, Any]) -> None:
    if not scenario:
        st.info("Não foi calculado um cenário financeiro para este caso.")
        return

    proposed = _number(
        scenario,
        "public_proposal_amount",
        "proposed_amount",
        "proposal_amount",
        "recommended_proposal_amount",
    )
    floor = _number(
        scenario,
        "private_reservation_amount",
        "negotiation_floor",
        "minimum_amount",
    )
    downside = _number(scenario, "downside_cost_amount")

    with st.container(horizontal=True):
        if proposed is not None:
            st.metric("Proposta inicial", _format_brl(proposed), border=True)
        if floor is not None:
            st.metric("Piso de negociação", _format_brl(floor), border=True)

    if downside:
        st.caption(f"Custo explícito informado para o cenário sem acordo: {_format_brl(downside)}.")

    st.warning(
        "O cenário usa somente valores confirmados e acréscimos legais explicitamente "
        "condicionados. Não calcula probabilidade de êxito, valor esperado ou indenização."
    )
    amount_rows = []
    for label, key in (
        ("Prejuízo direto confirmado", "direct_loss_amount"),
        ("Valor pago em cobrança contestada", "improper_payment_amount"),
        ("Incremento condicional do art. 42", "conditional_article_42_increment_amount"),
        ("Resultado ilustrativo inferior", "low_outcome_value"),
        ("Resultado ilustrativo superior", "high_outcome_value"),
    ):
        value = _number(scenario, key)
        if value is not None:
            amount_rows.append({"Componente": label, "Valor": _format_brl(value)})
    if amount_rows:
        st.table(amount_rows)
    source_rows = _financial_source_rows(scenario)
    if source_rows:
        with st.expander("Origem dos valores usados no cálculo"):
            st.caption("Cada componente mostra o fato confirmado ou o documento que o sustenta.")
            st.dataframe(source_rows, hide_index=True, width="stretch")

    if assumption := scenario.get("article_42_assumption"):
        st.caption(str(assumption))
    methodology = scenario.get("methodology")
    if methodology:
        st.markdown("**Como foi calculado**")
        for item in _as_text_list(methodology):
            st.markdown(f"- {item}")
    caveats = _as_text_list(scenario.get("caveats"))
    if caveats:
        st.markdown("**Limitações**")
        for item in caveats:
            st.markdown(f"- {item}")
    if calculation_hash := scenario.get("calculation_sha256"):
        st.caption(f"SHA-256 do cálculo: `{calculation_hash}`")


def _financial_source_rows(scenario: dict[str, Any]) -> list[dict[str, Any]]:
    """Flatten monetary component provenance for human-readable audit."""
    labels = {
        "direct_loss": "Prejuízo direto",
        "conditional_article_42": "Acréscimo condicional do art. 42",
        "downside_cost": "Custo do cenário sem acordo",
    }
    rows: list[dict[str, Any]] = []
    for component in scenario.get("components") or []:
        if not isinstance(component, dict):
            continue
        kind = str(component.get("kind") or "")
        amount = _number(component, "amount")
        for source in component.get("sources") or []:
            if not isinstance(source, dict):
                continue
            source_type = str(source.get("source_type") or "")
            filename = str(source.get("filename") or "")
            page = source.get("page")
            if filename:
                location = f"{filename}, p. {page}" if page else filename
            else:
                location = "Formulário confirmado"
            extraction = str(source.get("extraction_method") or "manual")
            if source.get("ocr_applied"):
                extraction = f"{extraction} + OCR"
            rows.append(
                {
                    "Componente": labels.get(kind, kind or "Valor"),
                    "Valor": _format_brl(amount) if amount is not None else "—",
                    "Incluído na proposta": bool(component.get("included_in_public_proposal")),
                    "Origem": (
                        "Documento"
                        if source_type == "evidence"
                        else "Fato confirmado pelo consumidor"
                    ),
                    "Arquivo/página": location,
                    "Trecho": str(source.get("quote") or ""),
                    "Extração": extraction,
                    "SHA-256 do arquivo": str(source.get("source_sha256") or "—"),
                    "SHA-256 do texto": str(source.get("content_sha256") or "—"),
                    "SHA-256 do trecho": str(source.get("quote_sha256") or "—"),
                }
            )
    return rows


def _render_consumer_audit(notice: dict[str, Any]) -> None:
    release = notice.get("corpus_release_id")
    if release:
        st.caption(f"Versão da base legal: `{release}`")
    policy_version = notice.get("legal_ground_policy_version")
    policy_review = notice.get("legal_ground_policy_review_status")
    if policy_version:
        st.caption(
            f"Política determinística de elegibilidade: `{policy_version}` · "
            f"revisão: `{policy_review or 'não informada'}`"
        )

    timing = notice.get("generation_timing")
    if isinstance(timing, dict):
        reused = "sim" if timing.get("evidence_index_reused") else "não"
        st.caption(
            "Tempo de geração — "
            f"índice de evidências: {_format_duration_ms(timing.get('evidence_index_ms'))} "
            f"(reutilizado: {reused}) · recuperação: "
            f"{_format_duration_ms(timing.get('retrieval_ms'))} · composição: "
            f"{_format_duration_ms(timing.get('composition_ms'))} · total: "
            f"{_format_duration_ms(timing.get('total_ms'))}"
        )

    rows = _retrieval_rows(notice.get("retrievals") or [])
    if rows:
        st.dataframe(
            rows,
            hide_index=True,
            width="stretch",
            column_config={
                "Rank": st.column_config.NumberColumn(format="%d"),
                "Score": st.column_config.NumberColumn(format="%.4f"),
                "No contexto": st.column_config.CheckboxColumn(),
                "Chunk": st.column_config.TextColumn(pinned=True),
            },
        )
    else:
        st.info("Nenhum registro de recuperação foi anexado ao rascunho.")

    assessments = notice.get("security_assessments") or []
    if assessments:
        st.markdown("**Varredura dos documentos**")
        for assessment in assessments:
            if not isinstance(assessment, dict):
                continue
            st.caption(
                f"{assessment.get('filename') or 'Documento'} · "
                f"risco {assessment.get('risk_level') or 'não informado'} · "
                f"ação {assessment.get('recommended_action') or 'não informada'}"
            )

    st.caption(
        "A auditoria identifica os trechos recuperados e se foram incluídos no "
        "contexto. O conteúdo dos arquivos é tratado como dado não confiável, nunca como "
        "instrução para o assistente."
    )


def _format_duration_ms(value: Any) -> str:
    try:
        milliseconds = float(value)
    except (TypeError, ValueError):
        return "—"
    if milliseconds < 1_000:
        return f"{milliseconds:.0f} ms"
    return f"{milliseconds / 1_000:.1f} s"


def _retrieval_rows(retrievals: Iterable[Any]) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for retrieval in retrievals:
        if not isinstance(retrieval, dict):
            continue
        query = retrieval.get("query") or "-"
        source = retrieval.get("source") or retrieval.get("agent") or "-"
        results = retrieval.get("results")
        if isinstance(results, list):
            for result in results:
                if isinstance(result, dict):
                    rows.append(_retrieval_row(source, query, result))
        else:
            rows.append(_retrieval_row(source, query, retrieval))
    return rows


def _retrieval_row(
    source: Any,
    query: Any,
    result: dict[str, Any],
) -> dict[str, Any]:
    page_start = result.get("page_start") or result.get("page")
    page_end = result.get("page_end") or page_start
    pages = (
        f"{page_start}-{page_end}"
        if page_start is not None and page_end != page_start
        else page_start or "-"
    )
    return {
        "Fonte": source,
        "Consulta": query,
        "Rank": result.get("rank") or result.get("retrieval_rank"),
        "Score": result.get("score") or result.get("retrieval_score"),
        "Chunk": result.get("chunk_id") or "-",
        "Páginas": pages,
        "No contexto": bool(result.get("included_in_context", True)),
        "Prévia": result.get("text_preview") or "-",
        "SHA-256": result.get("content_sha256") or "-",
    }


def _case_from_payload(payload: dict[str, Any]) -> dict[str, Any]:
    case = payload.get("case") or payload.get("snapshot") or payload
    if not isinstance(case, dict):
        raise ConsumerApiError("A API retornou um atendimento inválido.")
    return case


def _notice_from_payload(payload: dict[str, Any]) -> dict[str, Any]:
    notice = payload.get("notice") or payload
    if not isinstance(notice, dict):
        raise ConsumerApiError("A API retornou um rascunho inválido.")
    return notice


def _merge_assistant_message(case: dict[str, Any], assistant: Any) -> None:
    if not assistant:
        return
    if isinstance(assistant, dict):
        content = assistant.get("content") or assistant.get("text")
    else:
        content = str(assistant)
    if not content:
        return
    messages = case.setdefault("messages", [])
    if not isinstance(messages, list):
        messages = []
        case["messages"] = messages
    if not any(
        isinstance(item, dict)
        and (item.get("content") or item.get("text")) == content
        and str(item.get("role") or "").lower() == "assistant"
        for item in messages
    ):
        messages.append({"role": "assistant", "content": content})


def _store_case(case: dict[str, Any]) -> None:
    st.session_state.consumer_case = case
    if case_id := case.get("case_id"):
        st.session_state.consumer_case_id = str(case_id)


def _reset_case_state() -> None:
    for key in CONSUMER_STATE_KEYS:
        if key in st.session_state:
            del st.session_state[key]
    for key in list(st.session_state):
        if key.startswith("consumer_fact_") or key.startswith("consumer_evidence_"):
            del st.session_state[key]
    _initialize_state()


def _set_flash(kind: str, message: str) -> None:
    st.session_state.consumer_flash = {"kind": kind, "message": message}


def _render_flash() -> None:
    flash = st.session_state.pop("consumer_flash", None)
    if not isinstance(flash, dict):
        return
    renderer = {
        "success": st.success,
        "warning": st.warning,
        "error": st.error,
    }.get(flash.get("kind"), st.info)
    renderer(str(flash.get("message") or ""))


def _split_protocols(value: str) -> list[str]:
    return [item.strip() for item in re.split(r"[\n,;]+", value) if item.strip()]


def _as_text_list(value: Any) -> list[str]:
    if value is None:
        return []
    if isinstance(value, str):
        return [value]
    if isinstance(value, Iterable) and not isinstance(value, (dict, bytes)):
        return [str(item) for item in value if item]
    return [str(value)]


def _safe_generated_markdown(value: str) -> str:
    """Keep useful formatting while neutralizing HTML, embeds and foreign links.

    The notice legitimately links to Planalto for every cited provision, so
    links are filtered rather than removed wholesale: anything pointing
    elsewhere originated in an uploaded document and must not be clickable
    inside the consumer's own draft.
    """
    escaped = html.escape(value, quote=False)
    escaped = escaped.replace("![", "\\![")
    return _MARKDOWN_LINK_RE.sub(_neutralize_foreign_link, escaped)


def _neutralize_foreign_link(match: re.Match[str]) -> str:
    label, target = match.group(1), match.group(2)
    if _is_official_legal_url(target):
        return match.group(0)
    return f"{label} ({target})"


def _markdown_literal(value: object) -> str:
    """Render caller-supplied text as literal characters inside st.markdown."""
    return _MARKDOWN_LITERAL_RE.sub(r"\\\1", str(value))


def _is_official_legal_url(value: str) -> bool:
    try:
        parsed = urlparse(value)
    except ValueError:
        return False
    return parsed.scheme == "https" and (parsed.hostname or "").lower() in OFFICIAL_LEGAL_HOSTS


def _number(mapping: dict[str, Any], *keys: str) -> float | None:
    for key in keys:
        value = mapping.get(key)
        if value is None:
            continue
        try:
            return float(value)
        except (TypeError, ValueError):
            continue
    return None


def _format_brl(value: float) -> str:
    formatted = f"{value:,.2f}"
    return "R$ " + formatted.replace(",", "_").replace(".", ",").replace("_", ".")
