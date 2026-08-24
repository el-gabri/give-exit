"""AI Litigation Copilot - Streamlit frontend.

Pure API client: talks to the FastAPI backend over HTTP and imports nothing
from the backend codebase. If this UI can render it, any client can.

Run:
    streamlit run frontend/streamlit_app.py
"""

import os
import sys
from pathlib import Path

import requests
import streamlit as st

# The Streamlit launcher does not guarantee that the repository root is importable.
_PROJECT_ROOT = str(Path(__file__).resolve().parents[1])
if _PROJECT_ROOT not in sys.path:
    sys.path.insert(0, _PROJECT_ROOT)

from frontend.consumer_view import render_consumer_app  # noqa: E402

API_URL = os.getenv("LITIGATION_API_URL", "http://localhost:8000")
API_AUTH_KEY = os.getenv("LITIGATION_API_AUTH_KEY", "").strip() or None
POLL_SECONDS = 1.0

STAGE_LABELS = {
    "security_scan": "Verificando seguranca do documento",
    "index": "Indexando documento (RAG)",
    "classify": "Classificando a acao",
    "extract": "Extraindo dados estruturados",
    "analyze": "Analise juridica",
    "enrich": "Validando no DataJud",
    "risk": "Avaliando riscos",
    "strategy": "Organizando opcoes preliminares",
    "compose": "Montando relatorio",
}
RISK_LABELS = {"low": "Baixo", "medium": "Medio", "high": "Alto", "critical": "Critico"}
PRIORITY_ICONS = {
    "urgent": ":red[URGENTE]",
    "high": ":orange[Alta]",
    "medium": ":blue[Media]",
    "low": ":gray[Baixa]",
}
SECURITY_ACTION_LABELS = {
    "proceed": "Analise liberada",
    "proceed_with_warning": "Analise liberada com aviso e mascaramento",
    "human_review": "Analise interrompida para revisao humana",
    "block": "Analise automatizada bloqueada",
}

st.set_page_config(
    page_title="AI Litigation Copilot", page_icon=":material/balance:", layout="wide"
)

audience = st.segmented_control(
    "Como você quer usar o Copilot?",
    options=["Sou consumidor", "Sou empresa"],
    default="Sou empresa",
    required=True,
    key="audience_mode",
    width="stretch",
)

if audience == "Sou consumidor":
    render_consumer_app(API_URL, api_key=API_AUTH_KEY)
    st.stop()


# ---------------------------------------------------------------- helpers
def api_get(path: str, **kwargs):
    headers = dict(kwargs.pop("headers", {}))
    if API_AUTH_KEY:
        headers["X-API-Key"] = API_AUTH_KEY
    return requests.get(f"{API_URL}{path}", timeout=30, headers=headers, **kwargs)


def api_post(path: str, **kwargs):
    headers = dict(kwargs.pop("headers", {}))
    if API_AUTH_KEY:
        headers["X-API-Key"] = API_AUTH_KEY
    return requests.post(f"{API_URL}{path}", headers=headers, **kwargs)


def confidence_badge(conclusion: dict) -> str:
    pct = round(conclusion.get("confidence", 0) * 100)
    color = "green" if pct >= 75 else "orange" if pct >= 50 else "red"
    return f":{color}[{pct}% de suporte autoavaliado]"


def render_conclusion(conclusion: dict, key_prefix: str) -> None:
    st.markdown(f"**{conclusion['statement']}**  {confidence_badge(conclusion)}")
    with st.expander("Por que? (justificativa e fontes)"):
        st.write(conclusion.get("reasoning", ""))
        for citation in conclusion.get("citations", []):
            page = f" (p. {citation['page']})" if citation.get("page") else ""
            chunk = f" · chunk {citation['chunk_id']}" if citation.get("chunk_id") else ""
            st.caption(f'Fonte: "{citation["quote"]}"{page}{chunk}')


def render_retrieval_audit(report: dict) -> None:
    """Display ranked retrieval provenance using native Streamlit components."""
    retrievals = [
        retrieval
        for trace in report.get("traces", [])
        for retrieval in trace.get("retrievals", [])
    ]
    if not retrievals:
        st.info("Nenhuma recuperacao RAG foi executada nesta analise.")
        return

    metrics = report.get("metrics", {})
    with st.container(horizontal=True):
        st.metric("Consultas", metrics.get("retrieval_queries", 0), border=True)
        st.metric("Resultados", metrics.get("retrieval_results", 0), border=True)
        st.metric(
            "Trechos unicos",
            metrics.get("retrieval_unique_chunks", 0),
            border=True,
        )
        st.metric("No contexto", metrics.get("context_chunks", 0), border=True)
        st.metric(
            "Duracao RAG",
            f"{metrics.get('retrieval_duration_ms', 0):.0f} ms",
            border=True,
        )

    coverage = metrics.get("citation_retrieval_coverage")
    if coverage is not None:
        st.caption(
            f"Cobertura de citacoes vinculadas ao contexto recuperado: {coverage:.1%}"
        )

    failed_retrievals = [
        retrieval for retrieval in retrievals if retrieval.get("error")
    ]
    if failed_retrievals:
        st.warning(
            f"{len(failed_retrievals)} consulta(s) de recuperacao falharam. "
            "Os demais resultados do mesmo lote foram preservados para auditoria."
        )
        with st.expander("Detalhes das falhas de recuperacao"):
            for retrieval in failed_retrievals:
                st.write(
                    f"**{retrieval['agent']} · consulta "
                    f"{retrieval['query_index'] + 1}:** {retrieval['error']}"
                )

    context_only = st.toggle(
        "Mostrar apenas trechos enviados aos modelos",
        value=True,
        key="retrieval_context_only",
    )
    rows = []
    for retrieval in sorted(
        retrievals, key=lambda item: (item["agent"], item["query_index"])
    ):
        retrieval_results = retrieval.get("results", [])
        for item in retrieval_results:
            if context_only and not item.get("included_in_context"):
                continue
            rows.append(
                {
                    "Agente": retrieval["agent"],
                    "Status": retrieval.get("agent_status") or "-",
                    "Prompt": retrieval.get("prompt_version") or "-",
                    "Consulta": retrieval["query"],
                    "Rank": item["rank"],
                    "Rank combinado": item.get("merged_rank"),
                    "Score": item["score"],
                    "Chunk": item["chunk_id"],
                    "Secao": item.get("section") or "sem secao",
                    "Paginas": f"{item['page_start']}-{item['page_end']}",
                    "No contexto": item.get("included_in_context", False),
                    "Previa": item.get("text_preview") or "(retencao desativada)",
                    "SHA-256": item["content_sha256"],
                }
            )

        if not context_only and not retrieval_results:
            rows.append(
                {
                    "Agente": retrieval["agent"],
                    "Status": retrieval.get("agent_status") or "-",
                    "Prompt": retrieval.get("prompt_version") or "-",
                    "Consulta": retrieval["query"],
                    "Rank": None,
                    "Rank combinado": None,
                    "Score": None,
                    "Chunk": "-",
                    "Secao": "-",
                    "Paginas": "-",
                    "No contexto": False,
                    "Previa": "-",
                    "SHA-256": "-",
                }
            )

    if rows:
        st.dataframe(
            rows,
            hide_index=True,
            column_config={
                "Rank": st.column_config.NumberColumn(format="%d"),
                "Rank combinado": st.column_config.NumberColumn(format="%d"),
                "Score": st.column_config.NumberColumn(format="%.4f"),
                "No contexto": st.column_config.CheckboxColumn(),
                "Chunk": st.column_config.TextColumn(pinned=True),
            },
        )
    else:
        st.info("Nenhum trecho recuperado foi enviado aos modelos.")
    st.caption(
        "A tabela completa preserva o top-k bruto por consulta. As previas ficam "
        "desativadas por padrao; o hash identifica exatamente o texto indexado."
    )


def render_stages(stages: list[dict], container) -> None:
    icons = {
        "done": ":material/check_circle:",
        "running": ":material/progress_activity:",
        "pending": ":material/radio_button_unchecked:",
        "failed": ":material/error:",
        "skipped": ":material/do_not_disturb_on:",
    }
    lines = [
        f"{icons[s['state']]} {STAGE_LABELS.get(s['name'], s['name'])}"
        for s in stages
    ]
    container.markdown("\n\n".join(lines))


def render_security_assessment(report: dict) -> None:
    assessment = report.get("security_assessment")
    if not assessment:
        return

    level = assessment["risk_level"]
    action = SECURITY_ACTION_LABELS.get(
        assessment["recommended_action"], assessment["recommended_action"]
    )
    count = len(assessment.get("findings", []))
    message = (
        f"Seguranca do documento: risco {RISK_LABELS.get(level, level)}. "
        f"{action}. {count} achado(s)."
    )
    if not assessment.get("scan_complete", True) or level in ("high", "critical"):
        st.error(message, icon=":material/gpp_bad:")
    elif level == "medium":
        st.warning(message, icon=":material/warning:")
    elif assessment.get("detected"):
        st.info(message, icon=":material/shield:")
    else:
        st.success(
            "Varredura de prompt injection concluida sem achados.",
            icon=":material/verified_user:",
        )

    if findings := assessment.get("findings"):
        with st.expander("Ver achados de seguranca", expanded=level in ("high", "critical")):
            for finding in findings:
                with st.container(border=True):
                    pages = (
                        f"Paginas {finding['page']}-{finding['page_end']}"
                        if finding.get("page_end")
                        and finding["page_end"] != finding["page"]
                        else f"Pagina {finding['page']}"
                    )
                    st.markdown(
                        f"**{pages} · "
                        f"{finding['category']} · risco {finding['severity']}**"
                    )
                    st.code(finding["quote"], language=None, wrap_lines=True)
                    st.caption(
                        f"{finding['reasoning']} · detector {finding['source']} · "
                        f"confianca {round(finding['confidence'] * 100)}%"
                    )


@st.fragment(run_every=POLL_SECONDS)
def poll_analysis(job_id: str) -> None:
    """Refresh only job progress instead of blocking the whole Streamlit app."""
    try:
        status = api_get(f"/analyses/{job_id}").json()
        st.session_state["job_status"] = status
    except requests.RequestException as exc:
        st.error(f"Falha ao acompanhar a analise: {exc}")
        return

    with st.container(border=True):
        render_stages(status["stages"], st)

    if status["state"] == "failed":
        st.session_state["job_error"] = "; ".join(status["errors"])
        st.rerun()
    if status["state"] in (
        "succeeded",
        "partial",
        "review_required",
        "rejected",
        "blocked",
    ):
        try:
            response = api_get(f"/analyses/{job_id}/report")
            response.raise_for_status()
            st.session_state["report"] = response.json()
            st.session_state.pop("job_error", None)
        except requests.RequestException as exc:
            st.session_state["job_error"] = f"Falha ao carregar relatorio: {exc}"
        st.rerun()


# ---------------------------------------------------------------- sidebar
with st.sidebar:
    st.title("⚖️ Litigation Copilot")
    st.caption("Analise inicial de acoes judiciais com IA explicavel")
    try:
        api_get("/health").raise_for_status()
        st.success("API conectada")
        totals = api_get("/runs/totals").json()
        col1, col2 = st.columns(2)
        col1.metric("Analises", totals["runs"])
        col2.metric("Custo total", f"US$ {totals['total_cost_usd']:.3f}")
        if totals["runs"]:
            with st.expander("Historico de execucoes"):
                for run in api_get("/runs").json():
                    outcome = run.get("outcome") or (
                        "succeeded" if run["success"] else "failed"
                    )
                    status = {
                        "succeeded": "✅",
                        "blocked": "🛡️",
                        "review_required": "⚠️",
                    }.get(outcome, "❌")
                    st.caption(
                        f"{status} arquivo ref. {run['filename']} · "
                        f"{run['metrics']['total_tokens']} tokens · "
                        f"US$ {run['metrics']['total_cost_usd']:.4f}"
                    )
    except requests.RequestException:
        st.error(f"API indisponivel em {API_URL}")
        st.caption("Inicie com: `uvicorn app.api.main:app`")
        st.stop()

    st.divider()
    st.caption(
        "Relatorios gerados por IA como apoio a decisao. "
        "Nao substituem a analise de um advogado."
    )

# ---------------------------------------------------------------- main
st.title("Analise de peticao inicial")
st.info(
    "Informacao juridica para apoio a revisao humana, nao aconselhamento juridico. "
    "A analise usa a peticao enviada como evidencia e nao valida, por si so, "
    "legislacao, jurisprudencia, prazos ou probabilidade de resultado."
)
with st.form("analysis_upload", border=False):
    uploaded = st.file_uploader("Envie o PDF da peticao inicial", type=["pdf"])
    submitted = st.form_submit_button(
        "Analisar", type="primary", icon=":material/analytics:", width="stretch"
    )

if uploaded and submitted:
    try:
        response = api_post(
            "/analyses",
            files={"file": (uploaded.name, uploaded.getvalue(), "application/pdf")},
            timeout=60,
        )
        response.raise_for_status()
    except requests.RequestException as exc:
        st.error(f"Falha no upload: {exc}")
        st.stop()
    st.session_state["job_id"] = response.json()["job_id"]
    st.session_state.pop("report", None)
    st.session_state.pop("job_error", None)

if job_id := st.session_state.get("job_id"):
    if "report" not in st.session_state:
        st.subheader("Pipeline de agentes")
        if error := st.session_state.get("job_error"):
            st.error("A analise falhou: " + error)
            st.stop()
        poll_analysis(job_id)
        st.stop()

    report = st.session_state["report"]
    job_status = st.session_state.get("job_status", {})

    # ------------------------------------------------ header metrics
    st.divider()
    render_security_assessment(report)

    if job_status.get("state") == "review_required":
        st.warning(
            "A verificacao de seguranca interrompeu a automacao. Um revisor humano "
            "deve decidir se o processamento pode continuar. O nome informado abaixo "
            "e apenas uma declaracao para auditoria; este demo nao autentica papeis."
        )
        with st.form("security_review"):
            reviewer = st.text_input("Identificacao do revisor")
            comment = st.text_area("Justificativa da decisao (opcional)")
            approve = st.form_submit_button("Aprovar e continuar", type="primary")
            reject = st.form_submit_button("Rejeitar processamento")
        if approve or reject:
            if not reviewer.strip():
                st.error("Informe a identificacao do revisor.")
            else:
                try:
                    response = api_post(
                        f"/analyses/{job_id}/review",
                        json={
                            "approved": approve,
                            "reviewer": reviewer.strip(),
                            "comment": comment.strip() or None,
                        },
                        timeout=30,
                    )
                    response.raise_for_status()
                    st.session_state["job_status"] = response.json()
                    if approve:
                        st.session_state.pop("report", None)
                    st.rerun()
                except requests.RequestException as exc:
                    st.error(f"Falha ao registrar a revisao: {exc}")
    elif job_status.get("state") == "rejected":
        st.error("O revisor rejeitou a continuacao do processamento automatizado.")

    quality = report.get("evidence_quality") or {}
    if quality.get("status") == "human_review_required":
        st.warning(
            "A verificacao deterministica de fontes encontrou conclusoes sem citacao "
            "rastreavel. Este relatorio esta parcial e requer revisao juridica humana."
        )
    elif quality.get("status") == "passed":
        st.info(
            "As citacoes exibidas foram reconstruidas dos trechos de origem. Isso nao "
            "verifica inferencia, correcao juridica ou probabilidade de resultado."
        )

    col1, col2, col3, col4 = st.columns(4)
    col1.metric(
        "Suporte autoavaliado (nao calibrado)",
        f"{round(report['confidence_level'] * 100)}%",
    )
    lawsuit_type = (report.get("classification") or {}).get("lawsuit_type", "-")
    col2.metric("Tipo de acao", lawsuit_type)
    col3.metric("Custo", f"US$ {report['metrics']['total_cost_usd']:.4f}")
    col4.metric("Tokens", report["metrics"]["total_tokens"])

    for warning in report.get("warnings", []):
        if not warning.startswith("Seguranca:"):
            st.warning(warning)

    # ------------------------------------------------ tabs
    tab_summary, tab_risk, tab_strategy, tab_details, tab_ai = st.tabs(
        ["Resumo", "Riscos", "Opcoes preliminares", "Detalhes", "Explicabilidade"]
    )

    with tab_summary:
        st.subheader("Resumo Executivo")
        st.write(report["executive_summary"] or "(indisponivel)")
        if classification := report.get("classification"):
            render_conclusion(classification["conclusion"], "cls")
        if timeline := report.get("timeline"):
            st.subheader("Linha do Tempo")
            for event in timeline:
                st.markdown(f"- **{event.get('date') or 's/ data'}** - {event['description']}")
                if citation := event.get("citation"):
                    page = f"p. {citation['page']}" if citation.get("page") else "s/ pagina"
                    chunk = (
                        f" · chunk {citation['chunk_id']}"
                        if citation.get("chunk_id")
                        else ""
                    )
                    st.caption(f'Fonte: "{citation["quote"]}" · {page}{chunk}')
        if parties := (report.get("parties") or {}).get("parties"):
            st.subheader("Partes")
            st.table(
                [
                    {"Papel": p["role"], "Nome": p["name"], "Advogado": p.get("lawyer") or "-"}
                    for p in parties
                ]
            )

    with tab_risk:
        if risk := report.get("legal_risks"):
            level = risk["overall_level"]
            st.subheader(f"Nivel geral: {RISK_LABELS.get(level, level)}")
            render_conclusion(risk["overall"], "risk-overall")
            for i, item in enumerate(risk.get("risks", [])):
                with st.container(border=True):
                    st.markdown(f"**{item['title']}**")
                    st.caption(
                        f"Risco: {RISK_LABELS.get(item['level'], item['level'])}"
                    )
                    if item.get("financial_exposure"):
                        st.caption(f"Exposicao: {item['financial_exposure']}")
                    render_conclusion(item["conclusion"], f"risk-{i}")
        else:
            st.info("Avaliacao de risco indisponivel nesta execucao.")

    with tab_strategy:
        if strategy := report.get("suggested_strategy"):
            st.subheader("Opcoes preliminares para revisao")
            render_conclusion(strategy["overall_approach"], "strat")
            if defenses := strategy.get("defenses"):
                st.subheader("Linhas de defesa")
                for i, defense in enumerate(defenses):
                    basis = f" — {defense['legal_basis']}" if defense.get("legal_basis") else ""
                    st.markdown(f"**{defense['argument']}**{basis}")
                    render_conclusion(defense["assessment"], f"def-{i}")
            if settlement := report.get("possible_settlement"):
                st.subheader("Acordo")
                render_conclusion(settlement, "settle")
            if actions := strategy.get("next_actions"):
                st.subheader("Proximas acoes")
                for action in actions:
                    icon = PRIORITY_ICONS.get(action["priority"], action["priority"])
                    st.markdown(f"- {icon} {action['action']} — {action['rationale']}")
        else:
            st.info("Estrategia indisponivel nesta execucao.")

    with tab_details:
        if claims := report.get("main_claims"):
            st.subheader("Pedidos analisados")
            for i, claim in enumerate(claims):
                basis = (
                    f" (base legal alegada: {claim['legal_basis']})"
                    if claim.get("legal_basis")
                    else ""
                )
                st.markdown(f"**{claim['claim']}**{basis}")
                render_conclusion(claim["assessment"], f"claim-{i}")
        if evidence := report.get("evidence_found"):
            st.subheader("Provas identificadas")
            for i, item in enumerate(evidence):
                render_conclusion(item, f"ev-{i}")
        if missing := report.get("missing_information"):
            st.subheader("Informacoes ausentes")
            for item in missing:
                st.markdown(f"- ⚠️ {item}")

    with tab_ai:
        st.subheader("Como a IA chegou a estas conclusoes")
        st.text(report["ai_reasoning"])
        st.subheader("Execucao por agente")
        st.table(
            [
                {
                    "Agente": t["agent"],
                    "Status": t["status"],
                    "Duracao (ms)": round(t["duration_ms"]),
                    "Tokens": (t.get("llm_meta") or {}).get("usage", {}).get(
                        "prompt_tokens", 0
                    )
                    + (t.get("llm_meta") or {}).get("usage", {}).get(
                        "completion_tokens", 0
                    ),
                    "Prompt": (t.get("llm_meta") or {}).get("prompt_version") or "-",
                }
                for t in report.get("traces", [])
            ]
        )
        st.subheader("Auditoria de recuperacao")
        render_retrieval_audit(report)

    # ------------------------------------------------ downloads
    st.divider()
    import json

    col_md, col_pdf, col_docx, col_json = st.columns(4)
    doc_id = report["doc_id"]
    col_md.download_button(
        "Baixar Markdown",
        data=api_get(f"/analyses/{job_id}/report.md").text,
        file_name=f"relatorio_{doc_id}.md",
        mime="text/markdown",
        width="stretch",
    )
    col_pdf.download_button(
        "Baixar PDF",
        data=api_get(f"/analyses/{job_id}/report.pdf").content,
        file_name=f"relatorio_{doc_id}.pdf",
        mime="application/pdf",
        width="stretch",
    )
    col_docx.download_button(
        "Baixar DOCX",
        data=api_get(f"/analyses/{job_id}/report.docx").content,
        file_name=f"relatorio_{doc_id}.docx",
        mime="application/vnd.openxmlformats-officedocument"
        ".wordprocessingml.document",
        width="stretch",
    )
    col_json.download_button(
        "Baixar JSON",
        data=json.dumps(report, ensure_ascii=False, indent=2),
        file_name=f"relatorio_{doc_id}.json",
        mime="application/json",
        width="stretch",
    )
