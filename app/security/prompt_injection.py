"""Layered prompt-injection detection for untrusted document text.

Deterministic rules scan every page before any RAG or legal-agent call. In
balanced mode, a schema-constrained LLM reviews only bounded suspicious
passages. The LLM may add findings but never removes rule findings or chooses
the policy action.
"""

import base64
import binascii
import json
import re
import time
import unicodedata
from dataclasses import dataclass

from app.core.config import PromptInjectionScanMode
from app.core.logging import get_logger
from app.llm.base import LLMCallMetadata, LLMClient, TokenUsage
from app.prompts.security import PROMPT_INJECTION_REVIEW_PROMPT
from app.schemas.document import ParsedDocument
from app.schemas.security import (
    FindingSource,
    InjectionCategory,
    PromptInjectionAssessment,
    PromptInjectionFinding,
    SecurityAction,
    SecurityRiskLevel,
    SemanticPromptInjectionReview,
)
from app.schemas.trace import AgentStatus, AgentTrace

logger = get_logger(__name__)

MAX_CANDIDATES = 20
MAX_CANDIDATE_CHARS = 700
MAX_SEMANTIC_CONTEXT_CHARS = 12_000
PAGE_BOUNDARY_CHARS = 300
SEMANTIC_CONFIDENCE_THRESHOLD = 0.7
DEFAULT_STRICT_MAX_CHARS = 500_000
DEFAULT_STRICT_MAX_BATCHES = 64
ZERO_WIDTH_CHARS = frozenset(
    "\u00ad\u200b\u200c\u200d\u2060\ufeff"
    "\u202a\u202b\u202c\u202d\u202e\u2066\u2067\u2068\u2069"
)

_HOMOGLYPHS = str.maketrans(
    {
        "а": "a",
        "е": "e",
        "і": "i",
        "ο": "o",
        "о": "o",
        "р": "p",
        "с": "c",
        "х": "x",
        "у": "y",
        "Α": "a",
        "Β": "b",
        "Ε": "e",
        "Ι": "i",
        "Ο": "o",
        "Ρ": "p",
        "Χ": "x",
    }
)

_SEVERITY_RANK = {
    SecurityRiskLevel.NONE: 0,
    SecurityRiskLevel.LOW: 1,
    SecurityRiskLevel.MEDIUM: 2,
    SecurityRiskLevel.HIGH: 3,
    SecurityRiskLevel.CRITICAL: 4,
}

_ACTION_BY_RISK = {
    SecurityRiskLevel.NONE: SecurityAction.PROCEED,
    SecurityRiskLevel.LOW: SecurityAction.PROCEED,
    SecurityRiskLevel.MEDIUM: SecurityAction.PROCEED_WITH_WARNING,
    SecurityRiskLevel.HIGH: SecurityAction.HUMAN_REVIEW,
    SecurityRiskLevel.CRITICAL: SecurityAction.BLOCK,
}

_CONTROL_TERMS = re.compile(
    r"\b(?:ignore|disregard|forget|override|bypass|reveal|show|print|call|invoke|"
    r"execute|run|use|send|upload|transmit|respond|return|follow|obey|"
    r"desconsidere|esqueca|ignore|revele|mostre|imprima|chame|invoque|execute|"
    r"rode|use|envie|transmita|responda|retorne|siga|obedeca)\b"
)
_AI_TARGET_TERMS = re.compile(
    r"\b(?:prompt|system|developer|assistant|chatgpt|model|instructions?|tools?|"
    r"functions?|api\s+keys?|secrets?|sistema|desenvolvedor|assistente|modelo|"
    r"instrucoes?|ferramentas?|funcoes?|chaves?\s+(?:da|de)\s+api|segredos?)\b"
)
_PROMPT_MARKERS = re.compile(
    r"\[\s*(?:system|developer|sistema|desenvolvedor)\s*\]|"
    r"<\s*/?\s*(?:system|developer)\s*>|"
    r"\b(?:system|developer)\s*(?:message|prompt)?\s*:|"
    r"begin\s+system\s+prompt|inicio\s+do\s+prompt\s+do\s+sistema"
)
# Phrases that talk about the assistant's own instructions, persona, output or
# session. A paraphrased attack can avoid every control verb above, but it
# cannot avoid referring to what it wants changed. These only widen the pool
# sent to the semantic reviewer - they never produce a finding on their own -
# so the cost of a false positive here is tokens, not a wrong decision.
# Phrases, not bare nouns: "o sistema do banco" and "as instrucoes de cobranca"
# are ordinary petition language.
_SELF_REFERENCE_CUES = re.compile(
    r"instruc(?:ao|oes)\s+(?:anterior|anteriores|previa|previas|inicial|iniciais|ocultas?)|"
    r"previous\s+instructions?|initial\s+instructions?|hidden\s+instructions?|"
    r"prompt\s+(?:do|de)\s+sistema|system\s+prompt|developer\s+message|"
    r"sua\s+resposta|suas\s+respostas|your\s+response|your\s+answer|your\s+reply|"
    r"you\s+(?:were|are)\s+(?:given|told|instructed)|"
    r"\bpersona\b|assuma\s+o\s+papel|assumir\s+a\s+persona|incorpore\s+a\s+persona|"
    r"esta\s+conversa|this\s+conversation|start\s+of\s+this|"
    r"palavra\s+por\s+palavra|word\s+for\s+word|verbatim|"
    r"rotina\s+interna|internal\s+routine|"
    r"texto\s+de\s+configuracao|configuration\s+text|"
    r"suas\s+(?:regras|restricoes|politicas)|your\s+(?:rules|restrictions|policies)"
)
_BASE64_BLOCK = re.compile(
    r"(?<![A-Za-z0-9+/_-])[A-Za-z0-9+/_-]{48,}={0,2}(?![A-Za-z0-9+/_-])"
)
_HTML_COMMENT = re.compile(r"<!--[\s\S]{1,2000}?-->")


@dataclass(frozen=True)
class _Rule:
    rule_id: str
    category: InjectionCategory
    severity: SecurityRiskLevel
    pattern: re.Pattern[str]
    reasoning: str
    confidence: float


def _pattern(expression: str) -> re.Pattern[str]:
    return re.compile(expression, re.DOTALL)


_GAP = r"[\s\S]{0,100}?"
_SHORT_GAP = r"[\s\S]{0,50}?"

_RULES = (
    _Rule(
        "override_prior_instructions",
        InjectionCategory.INSTRUCTION_OVERRIDE,
        SecurityRiskLevel.HIGH,
        _pattern(
            r"\b(?:ignore|disregard|forget|bypass|override|desconsidere|esqueca|abandone)\b"
            + _GAP
            + r"(?:"
            r"\b(?:previous|prior|earlier|system|developer|initial|anteriores?|"
            r"previas?|precedentes?)\b"
            + _SHORT_GAP
            + r"\b(?:instructions?|prompts?|messages?|rules?|instrucoes?|mensagens?|regras?)\b"
            + r"|\b(?:instructions?|prompts?|messages?|rules?|instrucoes?|mensagens?|regras?)\b"
            + _SHORT_GAP
            + r"\b(?:previous|prior|earlier|system|developer|initial|anteriores?|"
            r"previas?|precedentes?)\b)"
        ),
        "O trecho tenta substituir instrucoes anteriores ou de maior prioridade.",
        0.99,
    ),
    _Rule(
        "override_above_context",
        InjectionCategory.INSTRUCTION_OVERRIDE,
        SecurityRiskLevel.HIGH,
        _pattern(
            r"\b(?:ignore|disregard|forget|desconsidere|esqueca)\b"
            + _SHORT_GAP
            + r"\b(?:everything|all|text|content|commands|tudo|texto|conteudo|comandos)\b"
            + _SHORT_GAP
            + r"\b(?:above|earlier|acima|anterior)\b"
        ),
        "O trecho manda descartar o contexto fornecido ao modelo.",
        0.98,
    ),
    _Rule(
        "role_reassignment",
        InjectionCategory.ROLE_MANIPULATION,
        SecurityRiskLevel.HIGH,
        _pattern(
            r"\b(?:you\s+are\s+now|act\s+as|pretend\s+to\s+be|assume\s+the\s+role\s+of|"
            r"voce\s+agora\s+e|aja\s+como|finja\s+ser|assuma\s+o\s+papel\s+de)\b"
            + _SHORT_GAP
            + r"\b(?:chatgpt|assistant|ai|model|system|developer|assistente|ia|modelo|"
            r"sistema|desenvolvedor)\b"
        ),
        "O trecho tenta redefinir o papel ou a identidade da IA.",
        0.98,
    ),
    _Rule(
        "secret_exfiltration",
        InjectionCategory.DATA_EXFILTRATION,
        SecurityRiskLevel.CRITICAL,
        _pattern(
            r"\b(?:reveal|show|print|display|return|expose|dump|leak|mostre|revele|"
            r"imprima|exiba|retorne|exponha|vaze)\b"
            + _GAP
            + r"\b(?:system\s+prompt|developer\s+message|hidden\s+instructions?|"
            r"api\s+keys?|access\s+tokens?|secrets?|prompt\s+(?:do|de)\s+sistema|"
            r"mensagem\s+do\s+desenvolvedor|instrucoes?\s+ocultas?|"
            r"chaves?\s+(?:da|de)\s+api|"
            r"tokens?\s+de\s+acesso|segredos?)\b"
        ),
        "O trecho solicita prompts internos, credenciais ou outros segredos.",
        0.995,
    ),
    _Rule(
        "tool_invocation",
        InjectionCategory.TOOL_MANIPULATION,
        SecurityRiskLevel.HIGH,
        _pattern(
            r"\b(?:call|invoke|run|execute|use|open|browse|access|chame|invoque|execute|"
            r"rode|use|abra|acesse)\b"
            + _GAP
            + r"\b(?:tool|function|shell|terminal|powershell|bash|curl|browser|webhook|"
            r"ferramenta|funcao|navegador)\b"
        ),
        "O trecho tenta fazer a IA acionar ferramentas ou codigo externo.",
        0.97,
    ),
    _Rule(
        "ai_directed_external_transfer",
        InjectionCategory.DATA_EXFILTRATION,
        SecurityRiskLevel.CRITICAL,
        _pattern(
            r"\b(?:assistant|chatgpt|model|ai|assistente|modelo|ia)\b"
            + _GAP
            + r"\b(?:send|upload|post|transmit|email|envie|transmita|publique|mande)\b"
            + _GAP
            + r"\b(?:entire\s+document|full\s+document|document|file|content|data|text|"
            r"documento\s+inteiro|documento|arquivo|conteudo|dados|texto)\b"
            + _GAP
            + r"(?:https?://|\bwebhook\b|\bendpoint\b|\burl\b)"
        ),
        "O trecho instrui explicitamente a IA a transferir dados para fora.",
        0.995,
    ),
    _Rule(
        "external_transfer",
        InjectionCategory.DATA_EXFILTRATION,
        SecurityRiskLevel.CRITICAL,
        _pattern(
            r"\b(?:send|upload|post|transmit|email|envie|transmita|publique|mande)\b"
            + _GAP
            + r"\b(?:system\s+prompt|developer\s+message|hidden\s+instructions?|"
            r"api\s+keys?|access\s+tokens?|secrets?|full\s+model\s+context|"
            r"prompt\s+(?:do|de)\s+sistema|mensagem\s+do\s+desenvolvedor|"
            r"instrucoes?\s+ocultas?|chaves?\s+(?:da|de)\s+api|"
            r"tokens?\s+de\s+acesso|"
            r"segredos?|contexto\s+completo\s+do\s+modelo)\b"
            + _GAP
            + r"(?:https?://|\bwebhook\b|\bendpoint\b|\be-?mail\b|\burl\b)"
        ),
        "O trecho tenta transferir dados do documento para um destino externo.",
        0.99,
    ),
    _Rule(
        "forced_machine_output",
        InjectionCategory.OUTPUT_MANIPULATION,
        SecurityRiskLevel.MEDIUM,
        _pattern(
            r"\b(?:respond|reply|output|return|answer|responda|retorne|produza)\b"
            + _SHORT_GAP
            + r"\b(?:only|apenas|somente)\b"
            + _SHORT_GAP
            + r"\b(?:json|xml|code|codigo|token|yaml|csv)\b"
        ),
        "O trecho tenta forcar um formato de saida tipico de uma resposta de IA.",
        0.92,
    ),
    _Rule(
        "skip_document_analysis",
        InjectionCategory.INSTRUCTION_OVERRIDE,
        SecurityRiskLevel.HIGH,
        _pattern(
            r"\b(?:do\s+not|don['’]?t|nao)\b"
            + _SHORT_GAP
            + r"\b(?:analyze|summarize|classify|extract|read|analise|resuma|classifique|"
            r"extraia|leia)\b"
            + _SHORT_GAP
            + r"\b(?:this|the|este|esta|o|a)\b"
            + _SHORT_GAP
            + r"\b(?:document|file|text|documento|arquivo|texto)\b"
        ),
        "O trecho instrui o modelo a nao analisar o documento recebido.",
        0.96,
    ),
    _Rule(
        "system_prompt_marker",
        InjectionCategory.ROLE_MANIPULATION,
        SecurityRiskLevel.MEDIUM,
        _PROMPT_MARKERS,
        "O trecho imita um marcador de mensagem de sistema.",
        0.9,
    ),
)

_STRONG_RULES = tuple(
    rule
    for rule in _RULES
    if _SEVERITY_RANK[rule.severity]
    >= _SEVERITY_RANK[SecurityRiskLevel.HIGH]
)


class PromptInjectionDetector:
    """Scan a parsed document and return a traceable policy assessment."""

    name = "prompt_injection_scan"

    def __init__(
        self,
        llm: LLMClient,
        mode: PromptInjectionScanMode = PromptInjectionScanMode.BALANCED,
        strict_max_chars: int = DEFAULT_STRICT_MAX_CHARS,
        strict_max_batches: int = DEFAULT_STRICT_MAX_BATCHES,
    ) -> None:
        self._llm = llm
        self._mode = mode
        self._strict_max_chars = strict_max_chars
        self._strict_max_batches = strict_max_batches

    @property
    def mode(self) -> str:
        return self._mode.value

    async def scan(
        self, document: ParsedDocument
    ) -> tuple[PromptInjectionAssessment, AgentTrace]:
        start = time.perf_counter()
        findings = scan_prompt_injection_rules(document)
        semantic_reviewed = False
        semantic_meta: LLMCallMetadata | None = None
        warnings: list[str] = []
        scan_complete = True
        trace_error: str | None = None

        candidates: list[tuple[int, str]] = []
        review_batches: list[list[tuple[int, str]]] = []
        if self._mode is PromptInjectionScanMode.BALANCED:
            candidates = _semantic_candidates(document, findings)
        elif self._mode is PromptInjectionScanMode.STRICT:
            document_chars = sum(len(page.text) for page in document.pages)
            if document_chars > self._strict_max_chars:
                scan_complete = False
                trace_error = (
                    f"strict character budget exceeded: {document_chars} > "
                    f"{self._strict_max_chars}"
                )
                warnings.append(
                    "A revisao semantica integral excedeu o limite configurado "
                    "de caracteres; o modo strict bloqueou a analise automatizada."
                )
            else:
                candidates = _all_semantic_candidates(document)

        if candidates:
            review_batches = _candidate_batches(candidates)
            if (
                self._mode is PromptInjectionScanMode.STRICT
                and len(review_batches) > self._strict_max_batches
            ):
                scan_complete = False
                trace_error = (
                    f"strict batch budget exceeded: {len(review_batches)} > "
                    f"{self._strict_max_batches}"
                )
                warnings.append(
                    "A revisao semantica integral excedeu o limite configurado "
                    "de chamadas; o modo strict bloqueou a analise automatizada."
                )
                candidates = []

        if candidates:
            semantic_findings: list[PromptInjectionFinding] = []
            semantic_metadata: list[LLMCallMetadata] = []
            try:
                for batch in review_batches:
                    batch_findings, batch_meta = await self._semantic_review(
                        document, batch
                    )
                    semantic_findings.extend(batch_findings)
                    semantic_metadata.append(batch_meta)
                findings = _deduplicate_findings([*findings, *semantic_findings])
                semantic_meta = _aggregate_metadata(semantic_metadata)
                semantic_reviewed = True
            except Exception as exc:
                findings = _deduplicate_findings([*findings, *semantic_findings])
                semantic_meta = _aggregate_metadata(semantic_metadata)
                logger.warning(
                    "prompt_injection_semantic_review_failed",
                    error_type=type(exc).__name__,
                )
                if self._mode is PromptInjectionScanMode.STRICT:
                    scan_complete = False
                    trace_error = f"{type(exc).__name__}: {exc}"
                    warnings.append(
                        "A revisao semantica integral de seguranca falhou; o modo "
                        "strict bloqueou a analise automatizada."
                    )
                else:
                    warnings.append(
                        "A revisao semantica de seguranca falhou; a decisao usa "
                        "apenas a varredura deterministica completa."
                    )
        elif self._mode is PromptInjectionScanMode.STRICT and scan_complete:
            semantic_reviewed = True

        assessment = build_assessment(
            findings=findings,
            scanned_pages=document.page_count,
            scan_mode=self._mode.value,
            semantic_reviewed=semantic_reviewed,
            scan_complete=scan_complete,
            warnings=warnings,
        )
        trace = AgentTrace(
            agent=self.name,
            status=(AgentStatus.SUCCESS if scan_complete else AgentStatus.FAILED),
            duration_ms=(time.perf_counter() - start) * 1000,
            llm_meta=semantic_meta,
            error=trace_error,
        )
        return assessment, trace

    async def _semantic_review(
        self, document: ParsedDocument, candidates: list[tuple[int, str]]
    ) -> tuple[list[PromptInjectionFinding], LLMCallMetadata]:
        candidate_blocks: list[str] = []
        for page, passage in candidates:
            block = json.dumps(
                {"page": page, "text": passage},
                ensure_ascii=False,
            )
            candidate_blocks.append(block)

        result = await self._llm.parse(
            system=PROMPT_INJECTION_REVIEW_PROMPT.system,
            user=PROMPT_INJECTION_REVIEW_PROMPT.render_user(
                candidates="[\n" + ",\n".join(candidate_blocks) + "\n]"
            ),
            schema=SemanticPromptInjectionReview,
            temperature=0.0,
            prompt_version=(
                f"{PROMPT_INJECTION_REVIEW_PROMPT.name}:"
                f"{PROMPT_INJECTION_REVIEW_PROMPT.version}"
            ),
        )
        pages = {page.number: page.text for page in document.pages}
        candidate_texts: dict[int, list[str]] = {}
        for page, passage in candidates:
            candidate_texts.setdefault(page, []).append(passage)
        validated: list[PromptInjectionFinding] = []
        for finding in result.data.findings:
            quote = finding.quote.strip()
            page_text = pages.get(finding.page)
            if (
                not page_text
                or quote not in page_text
                or not any(
                    quote in candidate
                    for candidate in candidate_texts.get(finding.page, [])
                )
                or finding.confidence < SEMANTIC_CONFIDENCE_THRESHOLD
                or finding.severity is SecurityRiskLevel.NONE
            ):
                continue
            severity = finding.severity
            if severity is SecurityRiskLevel.CRITICAL:
                severity = SecurityRiskLevel.HIGH
            validated.append(
                finding.model_copy(
                    update={
                        "quote": quote,
                        "page_end": None,
                        "severity": severity,
                        "source": FindingSource.SEMANTIC,
                        "rule_id": None,
                    }
                )
            )
        return validated, result.meta


def scan_prompt_injection_rules(
    document: ParsedDocument,
) -> list[PromptInjectionFinding]:
    """Apply deterministic rules to every page, retaining exact source spans."""
    findings: list[PromptInjectionFinding] = []
    for page in document.pages:
        canonical, positions = _canonical_with_positions(page.text)
        for rule in _RULES:
            for match in rule.pattern.finditer(canonical):
                quote = _source_quote(page.text, positions, match.start(), match.end())
                if rule.rule_id == "system_prompt_marker":
                    marker_start = positions[match.start()]
                    marker_end = positions[match.end() - 1] + 1
                    quote = _containing_instruction(
                        page.text, marker_start, marker_end
                    )
                source_window = _source_window(
                    page.text, positions, match.start(), match.end()
                )
                if not quote.strip() or _is_legal_false_positive(rule, source_window):
                    continue
                findings.append(
                    PromptInjectionFinding(
                        category=rule.category,
                        severity=rule.severity,
                        page=page.number,
                        quote=quote,
                        reasoning=rule.reasoning,
                        confidence=rule.confidence,
                        source=FindingSource.RULE,
                        rule_id=rule.rule_id,
                    )
                )
                if any(char in ZERO_WIDTH_CHARS for char in quote):
                    findings.append(
                        _obfuscated_finding(
                            page.number,
                            quote,
                            rule.severity,
                            "Caracteres invisiveis ocultam uma instrucao de controle da IA.",
                            "zero_width_payload",
                        )
                    )

        findings.extend(_scan_encoded_payloads(page.number, page.text))
        findings.extend(_scan_html_comments(page.number, page.text))
    findings.extend(_scan_page_boundaries(document))
    return _deduplicate_findings(findings)


def build_assessment(
    *,
    findings: list[PromptInjectionFinding],
    scanned_pages: int,
    scan_mode: str,
    semantic_reviewed: bool = False,
    scan_complete: bool = True,
    warnings: list[str] | None = None,
) -> PromptInjectionAssessment:
    """Derive risk and action in code from the strongest validated finding."""
    detected_risk = max(
        (finding.severity for finding in findings),
        key=_SEVERITY_RANK.__getitem__,
        default=SecurityRiskLevel.NONE,
    )
    risk = SecurityRiskLevel.CRITICAL if not scan_complete else detected_risk
    return PromptInjectionAssessment(
        detected=bool(findings),
        risk_level=risk,
        recommended_action=_ACTION_BY_RISK[risk],
        findings=findings,
        scanned_pages=scanned_pages,
        scan_complete=scan_complete,
        scan_mode=scan_mode,
        semantic_reviewed=semantic_reviewed,
        warnings=warnings or [],
    )


def failed_scan_assessment(
    *, document: ParsedDocument, scan_mode: str, error: Exception
) -> PromptInjectionAssessment:
    """Fail closed when the mandatory deterministic scan cannot complete."""
    return PromptInjectionAssessment(
        detected=False,
        risk_level=SecurityRiskLevel.CRITICAL,
        recommended_action=SecurityAction.BLOCK,
        scanned_pages=0,
        scan_complete=False,
        scan_mode=scan_mode,
        warnings=[
            "A varredura de prompt injection nao foi concluida; a analise "
            f"automatizada foi bloqueada ({type(error).__name__})."
        ],
    )


def _canonical_with_positions(text: str) -> tuple[str, list[int]]:
    canonical: list[str] = []
    positions: list[int] = []
    for index, original_char in enumerate(text):
        if original_char in ZERO_WIDTH_CHARS:
            continue
        translated = original_char.translate(_HOMOGLYPHS)
        for char in unicodedata.normalize("NFKD", translated):
            if unicodedata.combining(char):
                continue
            canonical.append(char.lower())
            positions.append(index)
    return "".join(canonical), positions


def _canonical(text: str) -> str:
    return _canonical_with_positions(text)[0]


def _source_quote(
    source: str, positions: list[int], canonical_start: int, canonical_end: int
) -> str:
    if not positions or canonical_start >= len(positions):
        return ""
    start = positions[canonical_start]
    end_position = positions[min(canonical_end - 1, len(positions) - 1)] + 1
    return source[start:end_position][:1_000]


def _source_window(
    source: str,
    positions: list[int],
    canonical_start: int,
    canonical_end: int,
    context_chars: int = 80,
) -> str:
    if not positions or canonical_start >= len(positions):
        return ""
    start = max(0, positions[canonical_start] - context_chars)
    end = min(
        len(source),
        positions[min(canonical_end - 1, len(positions) - 1)] + context_chars + 1,
    )
    return source[start:end]


def _is_legal_false_positive(rule: _Rule, source_window: str) -> bool:
    if rule.rule_id != "tool_invocation":
        return False
    canonical = _canonical(source_window)
    if re.search(
        r"\b(?:curl|shell|terminal|powershell|bash|browser|webhook|endpoint)\b",
        canonical,
    ):
        return False
    return bool(
        re.search(
            r"\b(?:ferramenta|funcao)\b[\s\S]{0,50}\b"
            r"(?:pje|eproc|e-saj|esaj|projudi)\b",
            canonical,
        )
    )


def _containing_instruction(
    source: str, marker_start: int, marker_end: int
) -> str:
    """Expand a role marker to its containing paragraph for safe masking."""
    preceding_break = source.rfind("\n\n", 0, marker_start)
    paragraph_start = preceding_break + 2 if preceding_break >= 0 else 0
    paragraph_end = source.find("\n\n", marker_end)
    if paragraph_end < 0:
        paragraph_end = len(source)
    start = (
        paragraph_start
        if paragraph_end - paragraph_start <= 1_000
        else marker_start
    )
    return source[start : min(paragraph_end, start + 1_000)].strip()


def _scan_encoded_payloads(page: int, text: str) -> list[PromptInjectionFinding]:
    findings: list[PromptInjectionFinding] = []
    for match in _BASE64_BLOCK.finditer(text):
        payload = match.group(0)
        try:
            padded = payload + "=" * (-len(payload) % 4)
            decoded = base64.b64decode(
                padded, altchars=b"-_", validate=True
            ).decode("utf-8")
        except (binascii.Error, UnicodeDecodeError, ValueError):
            continue
        matched_rule = _strongest_matching_rule(decoded)
        if matched_rule is None:
            continue
        findings.append(
            _obfuscated_finding(
                page,
                payload[:1_000],
                matched_rule.severity,
                "Um bloco base64 decodifica para uma instrucao de controle da IA.",
                "base64_payload",
            )
        )
    return findings


def _scan_page_boundaries(
    document: ParsedDocument,
) -> list[PromptInjectionFinding]:
    """Detect strong instructions split across adjacent extracted pages."""
    findings: list[PromptInjectionFinding] = []
    for left, right in zip(document.pages, document.pages[1:], strict=False):
        left_text = left.text[-PAGE_BOUNDARY_CHARS:]
        right_text = right.text[:PAGE_BOUNDARY_CHARS]
        combined = f"{left_text}\n{right_text}"
        canonical, positions = _canonical_with_positions(combined)
        left_boundary = len(_canonical(left_text))
        for rule in _STRONG_RULES:
            for match in rule.pattern.finditer(canonical):
                if not (match.start() < left_boundary < match.end()):
                    continue
                quote = _source_quote(
                    combined, positions, match.start(), match.end()
                )
                source_window = _source_window(
                    combined, positions, match.start(), match.end()
                )
                if not quote.strip() or _is_legal_false_positive(rule, source_window):
                    continue
                findings.append(
                    PromptInjectionFinding(
                        category=rule.category,
                        severity=rule.severity,
                        page=left.number,
                        page_end=right.number,
                        quote=quote,
                        reasoning=(
                            f"{rule.reasoning} A instrucao cruza o limite entre paginas."
                        ),
                        confidence=rule.confidence,
                        source=FindingSource.RULE,
                        rule_id=f"{rule.rule_id}_page_boundary",
                    )
                )
    return findings


def _scan_html_comments(page: int, text: str) -> list[PromptInjectionFinding]:
    findings: list[PromptInjectionFinding] = []
    for match in _HTML_COMMENT.finditer(text):
        matched_rule = _strongest_matching_rule(match.group(0))
        if matched_rule is None:
            continue
        findings.append(
            _obfuscated_finding(
                page,
                match.group(0)[:1_000],
                matched_rule.severity,
                "Um comentario oculto contem uma instrucao de controle da IA.",
                "html_comment_payload",
            )
        )
    return findings


def _strongest_matching_rule(text: str) -> _Rule | None:
    canonical = _canonical(text)
    matches = [rule for rule in _STRONG_RULES if rule.pattern.search(canonical)]
    return max(
        matches,
        key=lambda rule: _SEVERITY_RANK[rule.severity],
        default=None,
    )


def _obfuscated_finding(
    page: int,
    quote: str,
    decoded_severity: SecurityRiskLevel,
    reasoning: str,
    rule_id: str,
) -> PromptInjectionFinding:
    severity = (
        decoded_severity
        if _SEVERITY_RANK[decoded_severity] >= _SEVERITY_RANK[SecurityRiskLevel.HIGH]
        else SecurityRiskLevel.HIGH
    )
    return PromptInjectionFinding(
        category=InjectionCategory.OBFUSCATED_PAYLOAD,
        severity=severity,
        page=page,
        quote=quote,
        reasoning=reasoning,
        confidence=0.99,
        source=FindingSource.RULE,
        rule_id=rule_id,
    )


def _semantic_candidates(
    document: ParsedDocument, findings: list[PromptInjectionFinding]
) -> list[tuple[int, str]]:
    """Select the bounded set of passages the semantic reviewer will see.

    Rule findings are always included. The remaining budget is then shared
    round-robin across pages instead of first-come-first-served: filling it in
    document order let an attacker pad an early page with innocuous decoys that
    merely look suspicious, pushing a real payload on a later page out of the
    review entirely.
    """
    candidates: list[tuple[int, str]] = []
    seen: set[tuple[int, str]] = set()

    def add(page: int, passage: str) -> bool:
        clipped = passage.strip()[:MAX_CANDIDATE_CHARS]
        key = (page, _canonical(clipped))
        if not clipped or key in seen or len(candidates) >= MAX_CANDIDATES:
            return False
        candidates.append((page, clipped))
        seen.add(key)
        return True

    for finding in findings:
        add(finding.page, finding.quote)

    by_page: dict[int, list[str]] = {}
    for page in document.pages:
        passages = [
            match.group(0).strip()
            for match in re.finditer(r"[^.!?;]+[.!?;]?", page.text)
            if match.group(0).strip()
        ]
        for passage in passages:
            canonical, positions = _canonical_with_positions(passage)
            control_match = _CONTROL_TERMS.search(canonical)
            target_match = _AI_TARGET_TERMS.search(canonical)
            marker_match = _PROMPT_MARKERS.search(canonical)
            self_reference_match = _SELF_REFERENCE_CUES.search(canonical)
            focus_matches = [
                match
                for match in (
                    control_match,
                    target_match,
                    marker_match,
                    self_reference_match,
                )
                if match is not None
            ]
            if (control_match and target_match) or marker_match or self_reference_match:
                focus_start = min(match.start() for match in focus_matches)
                focus_end = max(match.end() for match in focus_matches)
                source_start = positions[focus_start]
                source_end = positions[focus_end - 1] + 1
                by_page.setdefault(page.number, []).append(
                    _focused_passage(passage, source_start, source_end)
                )

    # Round-robin: every page contributes its first candidate before any page
    # contributes its second, so no single page can consume the whole budget.
    for index in range(max((len(items) for items in by_page.values()), default=0)):
        for page_number in sorted(by_page):
            page_candidates = by_page[page_number]
            if index < len(page_candidates):
                add(page_number, page_candidates[index])
        if len(candidates) >= MAX_CANDIDATES:
            break
    return candidates


def _focused_passage(text: str, focus_start: int, focus_end: int) -> str:
    if len(text) <= MAX_CANDIDATE_CHARS:
        return text
    focus_middle = (focus_start + focus_end) // 2
    start = max(0, focus_middle - MAX_CANDIDATE_CHARS // 2)
    end = min(len(text), start + MAX_CANDIDATE_CHARS)
    start = max(0, end - MAX_CANDIDATE_CHARS)
    return text[start:end]


def _all_semantic_candidates(document: ParsedDocument) -> list[tuple[int, str]]:
    """Cover every page with overlapping passages for explicit strict mode."""
    candidates: list[tuple[int, str]] = []
    overlap = 100
    step = MAX_CANDIDATE_CHARS - overlap
    for page in document.pages:
        for start in range(0, len(page.text), step):
            passage = page.text[start : start + MAX_CANDIDATE_CHARS].strip()
            if passage:
                candidates.append((page.number, passage))
    return candidates


def _candidate_batches(
    candidates: list[tuple[int, str]],
) -> list[list[tuple[int, str]]]:
    batches: list[list[tuple[int, str]]] = []
    current: list[tuple[int, str]] = []
    used = 2  # JSON array delimiters
    for page, passage in candidates:
        block_size = len(
            json.dumps({"page": page, "text": passage}, ensure_ascii=False)
        )
        if current and used + block_size > MAX_SEMANTIC_CONTEXT_CHARS:
            batches.append(current)
            current = []
            used = 2
        current.append((page, passage))
        used += block_size + 1
    if current:
        batches.append(current)
    return batches


def _aggregate_metadata(metadata: list[LLMCallMetadata]) -> LLMCallMetadata | None:
    if not metadata:
        return None
    first = metadata[0]
    costs = [item.cost_usd for item in metadata if item.cost_usd is not None]
    return LLMCallMetadata(
        provider=first.provider,
        model=first.model,
        latency_ms=sum(item.latency_ms for item in metadata),
        usage=TokenUsage(
            prompt_tokens=sum(item.usage.prompt_tokens for item in metadata),
            completion_tokens=sum(item.usage.completion_tokens for item in metadata),
        ),
        cost_usd=sum(costs) if costs else None,
        prompt_version=first.prompt_version,
    )


def _deduplicate_findings(
    findings: list[PromptInjectionFinding],
) -> list[PromptInjectionFinding]:
    best: dict[
        tuple[int, int | None, str, InjectionCategory], PromptInjectionFinding
    ] = {}
    for finding in findings:
        key = (
            finding.page,
            finding.page_end,
            _canonical(finding.quote),
            finding.category,
        )
        current = best.get(key)
        if current is None or _SEVERITY_RANK[finding.severity] > _SEVERITY_RANK[
            current.severity
        ]:
            best[key] = finding
    return sorted(
        best.values(),
        key=lambda item: (
            -_SEVERITY_RANK[item.severity],
            item.page,
            item.category.value,
            item.quote,
        ),
    )
