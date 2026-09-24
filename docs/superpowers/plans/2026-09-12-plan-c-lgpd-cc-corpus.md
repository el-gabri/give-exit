# LGPD and Civil Code Corpus Expansion Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add the LGPD (whole law) and the Civil Code (indexed: PARTE GERAL and PARTE ESPECIAL › LIVRO I) to the versioned legal corpus and vector index. The CDC stays the anchor of every notice. All existing provenance guarantees are preserved.

**Architecture:**
- A generic Planalto statute parser driven by declarative `StatuteSpec`s (`app/consumer/statutes/`) replaces `cdc_snapshot.py` and reproduces the CDC byte for byte.
- The three snapshots get v3 manifests.
- The corpus gains hierarchy fields and an index scope.
- The eligibility policy and the ground selector gain the LGPD chapter rules, the CDC anchor, a cap of three complementary grounds, and a degraded-mode block.
- The default corpus switches to release v4 in one task near the end, followed by a single reindex that reuses the 472 existing vectors (Plan B).

**Tech Stack:** Python 3.10+ syntax, `html.parser`, Pydantic v2, pytest (`asyncio_mode = "auto"`), `urllib`, ruff, strict mypy, GitHub Actions.

**Spec:** `docs/superpowers/specs/2026-09-12-lgpd-codigo-civil-corpus-design.md`, sections 6.1–6.4 and 6.6–6.8.

**Delivery order:** this is the third plan. Plan A (`ground_selection.py`, notice-path evaluator) and Plan B (vector reuse) must already be merged. Everything here ships in one pull request, so the corpus release changes once.

## Global Constraints

- **CDC byte identity:** the generic parser reproduces the CDC exactly. Digest over the legacy article fields: `bf4b5660a266627354364867f9b7fbd590f7b679ad2ec21266da03a95c452ccb`.
- **CDC/CF chunk texts unchanged:** SHA-256 over the sorted chunk texts joined by `"\x1e"` must stay `50d79e182c6a08c5945182f708a64ba3a8d8e09d6ac19f9250462aeeade219ff`. This covers all 472 current chunks (7 CF, 465 CDC) and is what lets Plan B reuse their vectors.
- **Counts:**

  | | Provisions | Units | Status |
  |---|---:|---:|---|
  | CDC | 130 | — | 125 active |
  | LGPD | 80 | 464 | 73 active, 6 vetoed (arts. 28, 55, 56, 57, 58, 59), 1 revoked (art. 55-B) |
  | CC | 2,083 | 3,857 | 2,017 active, 65 revoked, 1 vetoed (art. 819-A) |
  | Default corpus (CF + CDC + LGPD + CC) | 2,300 | — | 2,222 active |

  - CC in index scope: 971 articles, 919 of them active.
  - LGPD citable (active, outside Caps. IV, VIII, IX, X): 41 provisions.
- **Parsed-text SHA-256** (`\n`-joined extracted paragraphs, before corrections):
  - CDC: `ecc3fcb838ad85fb54aaae9b70b7f3cd851cdd66da14018eccaab32af402b5c9`
  - LGPD: `8232caaef569007b54fa8f38678d5087d2e1f916af6298e2a3654f0416498b27`
  - CC: `cb60b801d600a771f539cf94be7d7990bafb5829924f1e753e4b5224db276802`

  If a fresh LGPD or CC download gives a different value, the law text changed since 2026-09-12. Stop and report; do not pin the new value silently.
- **Identifiers:**

  | Identifier | Value |
  |---|---|
  | Statute parser | `planalto-statute-parser-v1` |
  | Snapshot manifest schema | 3 |
  | Corpus-hash payload schema | 2 |
  | Chunking identity | `legal-hierarchy-v3:target=1200` |
  | Ground policy | `consumer-notice-scope-eligibility-v3` |
  | Query builder | `consumer-legal-three-query-v4` |
  | Corpus release | `br-consumer-law-<retrieved_on of the LGPD/CC snapshots>-v4` |
  | Golden dataset version | 1.2.0 |

- **Selection rules:**
  - The CC index scope is PARTE GERAL plus PARTE ESPECIAL › LIVRO I. Every other CC book is `audit_only`.
  - LGPD chapters IV, VIII, IX and X are never citable.
  - LGPD and CC grounds are complementary. A notice cites at most 3 of them, and only when at least one CDC ground is selected. If any legal trace is `lexical_only`, none are cited.
- **Acceptance gates:**
  - After the expansion, known-bad citations on the original 15 golden cases must be at most 4.
  - Retrieval CI thresholds stay unchanged. If a gate fails, stop and report the per-case metrics. Do not change a threshold or remove a case without the maintainer's approval.
- **Network:** only Task 5 contacts `planalto.gov.br`, through the refresher.
- **Tooling:**
  - Lint: `python -m ruff check app tests frontend`. Types: `python -m mypy app`.
  - `python` means the project virtualenv interpreter. Run every command from the repository root.
- **Commits:** Conventional Commits, each ending with:
  `Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>`
  `Claude-Session: https://claude.ai/code/session_017ENVjKHCXDicafzUtJYJzA`

## File Structure

| File | Responsibility |
|---|---|
| `app/consumer/schemas.py` (modify) | New `LegalSource` values, law-id map, widened id patterns, hierarchy and `index_scope` fields |
| `app/schemas/evaluation.py` (modify) | Golden and evaluation ids accept `lgpd`/`cc` |
| `app/consumer/statutes/__init__.py` (new) | Public surface of the statutes package |
| `app/consumer/statutes/spec.py` (new) | `StatuteSpec`, `TextCorrection`, `DivisionSelector` |
| `app/consumer/statutes/parser.py` (new) | Planalto HTML → paragraphs → `ParsedArticle`s; completeness validation |
| `app/consumer/statutes/snapshot.py` (new) | Manifest v3, integrity checks, `load_statute` |
| `app/consumer/statutes/registry.py` (new) | The CDC, LGPD and CC specs |
| `app/consumer/update_statute_snapshot.py` (new) | Maintainer refresher; replaces `update_cdc_snapshot.py` |
| `app/consumer/cdc_snapshot.py`, `app/consumer/update_cdc_snapshot.py` (delete) | Replaced by the statutes package |
| `app/consumer/data/{cdc,lgpd,cc}/` | Pinned HTML snapshots and manifests |
| `app/consumer/legal_corpus.py` (modify) | Provisions from every statute, hierarchy, index scope, hash v2, cached doc id, lazy default corpus |
| `app/consumer/legal_policy.py` (modify) | LGPD/CC eligibility and source-precedence helpers |
| `app/consumer/ground_selection.py` (modify) | Applies precedence, CDC anchor and degraded block |
| `app/consumer/retrieval.py`, `app/evaluation/consumer_runner.py` (modify) | `personal_data` subcategory, `CDC`-qualified anchors, query builder v4 |
| `eval_data/consumer_legal_retrieval/dataset.json` (modify) | v1.2.0: new target corpus and 6 cases |
| Docs and texts | ADR 0016; notes in ADRs 0012/0013; `architecture.md`; READMEs; frontend disclaimer; settlement caveat |

---

### Task 1: Legal ids and sources for the LGPD and the Civil Code

**Files:**
- Modify: `app/consumer/schemas.py` (`class LegalSource`; the `pattern=` of `LegalTextUnit.unit_id`, `LegalProvision.provision_id`, `LegalProvision.law_id`, `LegalAuthorityCitation.law_id`, `LegalAuthorityCitation.unit_id`; `LegalProvision._set_and_validate_hash`)
- Modify: `app/schemas/evaluation.py` (`_LEGAL_ID_PATTERN` and the message in `_validate_legal_id`)
- Test: `tests/test_legal_ids.py`

**Interfaces:**
- Produces:
  - `LegalSource.DATA_PROTECTION_LAW = "data_protection_law"` and `LegalSource.CIVIL_CODE = "civil_code"`.
  - `LAW_ID_BY_SOURCE: dict[LegalSource, str]`, mapping to `br-cf`, `br-cdc`, `br-lgpd` and `br-cc`.
  - Id pattern `^br-(cf|cdc|lgpd|cc)-[a-z0-9-]+$`; law-id pattern `^br-(cf|cdc|lgpd|cc)$`.
  - A `LegalProvision.provision_id` must start with `f"{law_id}-"`.

- [ ] **Step 1: Create the working branch**

```bash
git switch -c feat/lgpd-cc-corpus
```

- [ ] **Step 2: Write the failing tests**

Create `tests/test_legal_ids.py`:

```python
"""Stable ids for the LGPD and the Civil Code."""

from __future__ import annotations

from datetime import date

import pytest
from pydantic import ValidationError

from app.consumer.schemas import LegalProvision, LegalSource, LegalTextUnit, LegalUnitKind
from app.schemas.evaluation import ConsumerLegalRetrievalHit

_LGPD_URL = "https://www.planalto.gov.br/ccivil_03/_ato2015-2018/2018/lei/l13709compilado.htm"


def _provision(**overrides: object) -> LegalProvision:
    payload: dict[str, object] = {
        "provision_id": "br-lgpd-art-18",
        "source": LegalSource.DATA_PROTECTION_LAW,
        "source_name": "Lei Geral de Proteção de Dados Pessoais (Lei nº 13.709/2018)",
        "article": "art. 18",
        "citation_label": "LGPD, art. 18",
        "summary": "O titular dos dados pessoais tem direitos perante o controlador.",
        "official_url": _LGPD_URL,
        "corpus_release_id": "test-release",
        "verified_on": date(2026, 9, 12),
    }
    payload.update(overrides)
    return LegalProvision.model_validate(payload)


def test_lgpd_and_civil_code_provisions_get_their_own_law_ids() -> None:
    civil = _provision(
        provision_id="br-cc-art-1358-a",
        source=LegalSource.CIVIL_CODE,
        article="art. 1.358-A",
        citation_label="Código Civil, art. 1.358-A",
    )
    unit = LegalTextUnit(
        unit_id="br-cc-art-1358-a-paragrafo-1",
        kind=LegalUnitKind.PARAGRAPH,
        label="§ 1",
        text="§ 1º Texto de teste.",
    )

    assert _provision().law_id == "br-lgpd"
    assert civil.law_id == "br-cc"
    assert unit.unit_id == "br-cc-art-1358-a-paragrafo-1"


def test_a_provision_id_must_belong_to_its_law() -> None:
    with pytest.raises(ValidationError, match="must start with the law id"):
        _provision(provision_id="br-cdc-art-18")


def test_evaluation_hits_accept_the_new_laws_only() -> None:
    hit = ConsumerLegalRetrievalHit(
        provision_id="br-lgpd-art-18", unit_id="br-lgpd-art-18-inciso-vi"
    )

    assert hit.retrieval_id == "br-lgpd-art-18-inciso-vi"
    with pytest.raises(ValidationError, match="stable lowercase legal id"):
        ConsumerLegalRetrievalHit(provision_id="br-ctn-art-1")
```

- [ ] **Step 3: Run the tests to verify they fail**

Run: `python -m pytest tests/test_legal_ids.py -q`
Expected: FAIL with `AttributeError: DATA_PROTECTION_LAW`.

- [ ] **Step 4: Implement**

In `app/consumer/schemas.py`:

1. Replace `class LegalSource` with:

```python
class LegalSource(str, Enum):
    FEDERAL_CONSTITUTION = "federal_constitution"
    CONSUMER_DEFENSE_CODE = "consumer_defense_code"
    DATA_PROTECTION_LAW = "data_protection_law"
    CIVIL_CODE = "civil_code"


LAW_ID_BY_SOURCE: dict[LegalSource, str] = {
    LegalSource.FEDERAL_CONSTITUTION: "br-cf",
    LegalSource.CONSUMER_DEFENSE_CODE: "br-cdc",
    LegalSource.DATA_PROTECTION_LAW: "br-lgpd",
    LegalSource.CIVIL_CODE: "br-cc",
}
_LEGAL_ID_PATTERN = r"^br-(cf|cdc|lgpd|cc)-[a-z0-9-]+$"
_LAW_ID_PATTERN = r"^br-(cf|cdc|lgpd|cc)$"
```

2. Replace every `pattern=r"^br-(cf|cdc)-[a-z0-9-]+$"` with `pattern=_LEGAL_ID_PATTERN`, and every `pattern=r"^br-(cf|cdc)$"` with `pattern=_LAW_ID_PATTERN`. There are five occurrences in total: `LegalTextUnit.unit_id`, `LegalProvision.provision_id`, `LegalProvision.law_id`, `LegalAuthorityCitation.law_id`, `LegalAuthorityCitation.unit_id`.
3. In `LegalProvision._set_and_validate_hash`, replace the block

```python
        if self.law_id is None:
            object.__setattr__(
                self,
                "law_id",
                ("br-cf" if self.source is LegalSource.FEDERAL_CONSTITUTION else "br-cdc"),
            )
```

with

```python
        if self.law_id is None:
            object.__setattr__(self, "law_id", LAW_ID_BY_SOURCE[self.source])
        if not self.provision_id.startswith(f"{self.law_id}-"):
            raise ValueError("provision_id must start with the law id")
```

In `app/schemas/evaluation.py`, change `_LEGAL_ID_PATTERN` to

```python
_LEGAL_ID_PATTERN = re.compile(r"^br-(?:cdc|cf|lgpd|cc)-art-[a-z0-9]+(?:-[a-z0-9]+)*$")
```

and, in `_validate_legal_id`, change `"stable lowercase CDC/CF id such as "` to `"stable lowercase legal id such as "`.

- [ ] **Step 5: Run the tests to verify they pass**

Run: `python -m pytest tests/test_legal_ids.py tests/test_consumer_legal_corpus.py tests/test_consumer_evaluation.py tests/test_consumer_schemas.py -q`
Expected: all pass.

- [ ] **Step 6: Lint, type-check, commit**

Run: `python -m ruff check app tests frontend` and `python -m mypy app`. Expected: clean.

```bash
git add app/consumer/schemas.py app/schemas/evaluation.py tests/test_legal_ids.py
git commit -F - <<'EOF'
feat(consumer): accept stable ids for the LGPD and the Civil Code

Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>
Claude-Session: https://claude.ai/code/session_017ENVjKHCXDicafzUtJYJzA
EOF
```

---

### Task 2: Generic statute spec and parser

**Files:**
- Create: `app/consumer/statutes/__init__.py`, `app/consumer/statutes/spec.py`, `app/consumer/statutes/parser.py`
- Test: `tests/test_statute_parser.py`

Nothing is wired to the corpus yet. `app/consumer/cdc_snapshot.py` keeps working until Task 3 deletes it.

**Interfaces:**
- Produces, in `app.consumer.statutes`:
  - `TextCorrection(find: str, replace: str, reason: str)`
  - `DivisionSelector(path: tuple[tuple[str, str], ...])`
  - `StatuteSpec(law_id, source, source_name, citation_prefix, source_url, directory, snapshot_file, encoding, last_article, expected_article_count, required_articles=frozenset(), known_absent=MappingProxyType({}), text_corrections=(), index_scope=None)`
  - `STATUTE_PARSER_VERSION = "planalto-statute-parser-v1"`
  - `HIERARCHY_LEVELS = ("part", "book", "title", "subtitle", "chapter", "section", "subsection")`
  - `StatuteParseError(ValueError)`
  - `ParsedArticle` (frozen) with `number`, `suffix`, `article_key`, `article_label`, `provision_id`, `part`, `book`, `title`, `subtitle`, `chapter`, `section`, `subsection`, `official_text`, `status`, `units`
  - `extract_paragraphs(html: str) -> list[str]`
  - `parsed_text_sha256(paragraphs: Sequence[str]) -> str`
  - `match_article_heading(paragraph: str) -> tuple[int, str | None] | None`
  - `parse_statute(spec: StatuteSpec, paragraphs: Sequence[str]) -> tuple[ParsedArticle, ...]`
  - `division_numeral(label: str | None, keyword: str) -> str`
  - `in_index_scope(spec: StatuteSpec, article: ParsedArticle) -> bool`

- [ ] **Step 1: Write the failing tests**

Create `tests/test_statute_parser.py`:

```python
"""The generic Planalto statute parser, rule by rule, on minimal pages."""

from __future__ import annotations

from types import MappingProxyType

import pytest

from app.consumer.schemas import LegalSource, LegalUnitKind, ProvisionStatus
from app.consumer.statutes import (
    DivisionSelector,
    ParsedArticle,
    StatuteParseError,
    StatuteSpec,
    TextCorrection,
    division_numeral,
    extract_paragraphs,
    in_index_scope,
    match_article_heading,
    parse_statute,
    parsed_text_sha256,
)
from app.consumer.statutes.parser import _OfficialHtmlTextParser

_SIGNATURE = "Brasília, 1 de janeiro de 2020; 199º da Independência."
_BODY = ("Art. 1º Um.", "Art. 2º Dois.", "Art. 3º Três.")


def _spec(**overrides: object) -> StatuteSpec:
    values: dict[str, object] = {
        "law_id": "br-cc",
        "source": LegalSource.CIVIL_CODE,
        "source_name": "Lei de teste",
        "citation_prefix": "Teste",
        "source_url": "https://www.planalto.gov.br/teste.htm",
        "directory": "teste",
        "snapshot_file": "teste.html",
        "encoding": "utf-8",
        "last_article": 3,
        "expected_article_count": 3,
    }
    values.update(overrides)
    return StatuteSpec(**values)  # type: ignore[arg-type]


def _parse(*paragraphs: str, **overrides: object) -> tuple[ParsedArticle, ...]:
    return parse_statute(_spec(**overrides), list(paragraphs))


def test_headings_nest_reset_and_take_their_names() -> None:
    articles = _parse(
        "Presidência da República",
        "PARTE GERAL",
        "LIVRO I",
        "DAS PESSOAS",
        "TÍTULO I Das Coisas",
        "CAPÍTULO I Disposições Gerais",
        "Art. 1 o Texto um.",
        "SUBTÍTULO I Do Casamento",
        "Seção I",
        "(Incluído pela Lei nº 1, de 2020)",
        "Da Seção Nomeada",
        "Art. 2º Texto dois.",
        "Art. 2-A. Texto dois A.",
        "Art. 2-B. Texto dois B.",
        "CAPITULO II DO FIM",
        "Art. 3º Texto três.",
        _SIGNATURE,
        "Texto depois da assinatura é ignorado.",
        expected_article_count=5,
        required_articles=frozenset({"2-a", "2-b"}),
    )

    first, second, second_a, _, third = articles
    assert (first.part, first.book, first.title, first.chapter) == (
        "PARTE GERAL",
        "LIVRO I DAS PESSOAS",
        "TÍTULO I Das Coisas",
        "CAPÍTULO I Disposições Gerais",
    )
    assert (second.subtitle, second.chapter, second.section) == (
        "SUBTÍTULO I Do Casamento",
        None,
        "Seção I Da Seção Nomeada",
    )
    assert (second_a.provision_id, second_a.article_key, second_a.article_label) == (
        "br-cc-art-2-a",
        "2-a",
        "art. 2-A",
    )
    assert (third.chapter, third.section) == ("CAPITULO II DO FIM", None)
    assert third.official_text == "Art. 3º Texto três."


def test_article_headings_accept_thousands_ordinals_and_a_parenthesis() -> None:
    assert match_article_heading("Art. 1.358-A. Texto") == (1358, "A")
    assert match_article_heading("Art. 3 o São absolutamente") == (3, None)
    assert match_article_heading("Art. 1º O presente") == (1, None)
    assert match_article_heading("Art. 759.(Revogado pela Lei nº 1, de 2020)") == (759, None)
    assert match_article_heading("Art 1.636. Sem ponto") is None
    assert match_article_heading("“Art. 7º citado") is None


def test_thousands_are_kept_in_labels_but_not_in_ids() -> None:
    paragraphs = [f"Art. {number:,}. Texto.".replace(",", ".") for number in range(1, 1002)]

    articles = parse_statute(
        _spec(last_article=1001, expected_article_count=1001), [*paragraphs, _SIGNATURE]
    )

    assert (articles[-1].provision_id, articles[-1].article_label) == (
        "br-cc-art-1001",
        "art. 1.001",
    )


def test_known_absences_bridge_the_sequence() -> None:
    articles = _parse(
        "Art. 1º Um.",
        "Art. 3º Três.",
        _SIGNATURE,
        expected_article_count=2,
        known_absent=MappingProxyType({2: "revogado em bloco"}),
    )

    assert [article.number for article in articles] == [1, 3]


def test_an_article_out_of_sequence_is_an_error() -> None:
    with pytest.raises(StatuteParseError, match="out of sequence after art. 1"):
        _parse("Art. 1º Um.", "Art. 3º Três.", _SIGNATURE)
    with pytest.raises(StatuteParseError, match="out of sequence after art. 1"):
        _parse("Art. 1º Um.", "Art. 2-A. Dois A.", _SIGNATURE)
    with pytest.raises(StatuteParseError, match="out of sequence after the start"):
        _parse("Art. 2º Dois.", _SIGNATURE)


def test_unexpected_text_after_a_heading_is_an_error() -> None:
    with pytest.raises(StatuteParseError, match="unexpected paragraph after heading"):
        _parse("CAPÍTULO I Disposições", "texto solto sem artigo", *_BODY, _SIGNATURE)


def test_corrections_must_apply_exactly_once() -> None:
    fix = TextCorrection("P A R T E G E R A L", "PARTE GERAL", "letras espaçadas")

    articles = _parse("P A R T E G E R A L", *_BODY, _SIGNATURE, text_corrections=(fix,))

    assert articles[0].part == "PARTE GERAL"
    with pytest.raises(StatuteParseError, match="matched 0 paragraphs"):
        _parse(*_BODY, _SIGNATURE, text_corrections=(fix,))
    with pytest.raises(StatuteParseError, match="matched 2 paragraphs"):
        _parse(
            "P A R T E G E R A L", "P A R T E G E R A L", *_BODY, _SIGNATURE,
            text_corrections=(fix,),
        )


def test_the_signature_is_required_and_may_have_a_space_before_the_comma() -> None:
    assert len(_parse(*_BODY, "Brasília , 14 de agosto de 2018.")) == 3
    with pytest.raises(StatuteParseError, match="signature"):
        _parse(*_BODY)


def test_completeness_checks_required_suffixes_and_the_count() -> None:
    with pytest.raises(StatuteParseError, match="missing suffixed articles"):
        _parse(*_BODY, _SIGNATURE, required_articles=frozenset({"2-a"}))
    with pytest.raises(StatuteParseError, match="3 articles, expected 4"):
        _parse(*_BODY, _SIGNATURE, expected_article_count=4)


def test_units_statuses_and_every_block_type() -> None:
    articles = _parse(
        "Art. 1º Caput do primeiro:",
        "I - primeiro inciso;",
        "a) primeira alínea;",
        "I - inciso repetido;",
        "§ 1º Primeiro parágrafo:",
        "II - inciso do parágrafo:",
        "b) alínea do inciso do parágrafo;",
        "Parágrafo único. Parágrafo único.",
        "Pena - detenção.",
        "“§ 4º Parágrafo de outra lei.",
        "“IV - Inciso de outra lei.",
        "“Art. 5º Artigo de outra lei.",
        "(Vide Lei nº 1, de 2020)",
        "Texto sem marcador.",
        "Art. 2º (VETADO).",
        "Art. 3º (Revogado pela Lei nº 2, de 2021).",
        _SIGNATURE,
    )

    first, vetoed, revoked = articles
    assert [unit.kind for unit in first.units] == [
        LegalUnitKind.CAPUT,
        LegalUnitKind.INCISO,
        LegalUnitKind.ALINEA,
        LegalUnitKind.INCISO,
        LegalUnitKind.PARAGRAPH,
        LegalUnitKind.INCISO,
        LegalUnitKind.ALINEA,
        LegalUnitKind.PARAGRAPH,
        LegalUnitKind.PENALTY,
        LegalUnitKind.QUOTED_AMENDMENT,
        LegalUnitKind.QUOTED_AMENDMENT,
        LegalUnitKind.QUOTED_AMENDMENT,
        LegalUnitKind.NOTE,
        LegalUnitKind.NORMATIVE_OTHER,
    ]
    ids = {unit.unit_id for unit in first.units}
    assert {"br-cc-art-1-inciso-i-2", "br-cc-art-1-paragrafo-1-inciso-ii-alinea-b"} <= ids
    assert (first.status, vetoed.status, revoked.status) == (
        ProvisionStatus.ACTIVE,
        ProvisionStatus.VETOED,
        ProvisionStatus.REVOKED,
    )


def test_division_numerals_and_index_scope() -> None:
    assert division_numeral("PARTE ESPECIAL", "parte") == "especial"
    assert division_numeral("LIVRO I DO DIREITO DAS OBRIGAÇÕES", "livro") == "i"
    assert division_numeral("TÍTULO I-A (Incluído pela Lei nº 1) DA EMPRESA", "titulo") == "i-a"
    assert division_numeral(None, "parte") == ""
    assert division_numeral("Sem numeral", "livro") == ""
    scoped = _spec(
        index_scope=(
            DivisionSelector((("parte", "geral"),)),
            DivisionSelector((("parte", "especial"), ("livro", "i"))),
        )
    )
    business = _parse("PARTE ESPECIAL", "LIVRO II Do Direito de Empresa", *_BODY, _SIGNATURE)
    obligations = _parse("PARTE ESPECIAL", "LIVRO I DO DIREITO DAS OBRIGAÇÕES", *_BODY, _SIGNATURE)

    assert not in_index_scope(scoped, business[0])
    assert in_index_scope(scoped, obligations[0])
    assert in_index_scope(_spec(), business[0])


def test_paragraph_extraction_ignores_scripts_and_joins_line_breaks() -> None:
    html = (
        "<html><head><style>p { color: red }</style>"
        "<script>var x = '<p>oculto</p>';</script></head><body>"
        "texto fora de parágrafo</p><br><p>Art. 1º&nbsp;Um<br>linha dois</p><p> </p>"
        "<div><p>Art. 2º Dois.</p></div></body></html>"
    )

    paragraphs = extract_paragraphs(html)

    assert paragraphs == ["Art. 1º Um linha dois", "Art. 2º Dois."]
    assert len(parsed_text_sha256(paragraphs)) == 64


def test_tags_inside_ignored_blocks_are_skipped() -> None:
    parser = _OfficialHtmlTextParser()
    parser.handle_starttag("script", [])
    parser.handle_starttag("p", [])
    parser.handle_data("oculto")
    parser.handle_endtag("p")
    parser.handle_endtag("script")

    assert parser.paragraphs == []
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `python -m pytest tests/test_statute_parser.py -q`
Expected: collection error `ModuleNotFoundError: No module named 'app.consumer.statutes'`.

- [ ] **Step 3: Create `spec.py`**

Create `app/consumer/statutes/spec.py`:

```python
"""Declarative description of one official statute snapshot."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from types import MappingProxyType

from app.consumer.schemas import LegalSource


@dataclass(frozen=True, slots=True)
class TextCorrection:
    """An exact fix for a typographic quirk of an official page.

    ``find`` must prefix exactly one extracted paragraph. A correction that no
    longer applies fails the parse, so a page Planalto has fixed is noticed and
    reviewed instead of silently changing the text.
    """

    find: str
    replace: str
    reason: str


@dataclass(frozen=True, slots=True)
class DivisionSelector:
    """A path of (level, numeral) pairs, e.g. (("parte", "especial"), ("livro", "i"))."""

    path: tuple[tuple[str, str], ...]


@dataclass(frozen=True, slots=True)
class StatuteSpec:
    """What a snapshot of one statute must contain, and how to read it."""

    law_id: str
    source: LegalSource
    source_name: str
    citation_prefix: str
    source_url: str
    directory: str
    snapshot_file: str
    encoding: str
    last_article: int
    expected_article_count: int
    required_articles: frozenset[str] = frozenset()
    known_absent: Mapping[int, str] = field(default_factory=lambda: MappingProxyType({}))
    text_corrections: tuple[TextCorrection, ...] = ()
    # ``None`` indexes the whole statute; otherwise only matching divisions.
    index_scope: tuple[DivisionSelector, ...] | None = None
```

- [ ] **Step 4: Create `parser.py`**

Create `app/consumer/statutes/parser.py`. Five blocks are **moved verbatim** from `app/consumer/cdc_snapshot.py`; copy them exactly as they are:
- the regexes `_PARAGRAPH_RE`, `_INCISO_RE`, `_ALINEA_RE`, `_PENALTY_RE`, `_QUOTED_AMENDMENT_RE`, `_EDITORIAL_NOTE_RE`, `_VETOED_RE`, `_REVOKED_RE` (lines 37–55);
- the classes `_ParagraphBuilder` and `_OfficialHtmlTextParser` (lines 168–209);
- the functions `_build_units`, `_unstructured_unit_kind`, `_quoted_unit_hierarchy`, `_status_for_text` (lines 410–529);
- the functions `_normalize_text` and `_strip_accents` (lines 555–565).

Everything else is new:

```python
"""Parse official compiled statutes published by Planalto.

The pages are legacy HTML: each rendered paragraph is a ``<p>``, and headings
and article headings must be recognized from text. The rules here are the same
for every statute; what differs between laws (last article, known gaps, page
quirks, index scope) is declared in a ``StatuteSpec``. Parsing fails closed:
a paragraph the rules cannot place is an error, never silently dropped or glued
to the wrong article.
"""

from __future__ import annotations

import hashlib
import re
import unicodedata
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from html.parser import HTMLParser

from app.consumer.schemas import LegalTextUnit, LegalUnitKind, ProvisionStatus
from app.consumer.statutes.spec import StatuteSpec

STATUTE_PARSER_VERSION = "planalto-statute-parser-v1"
HIERARCHY_LEVELS = ("part", "book", "title", "subtitle", "chapter", "section", "subsection")

_LEVEL_BY_KEYWORD = {
    "PARTE": "part",
    "LIVRO": "book",
    "TITULO": "title",
    "SUBTITULO": "subtitle",
    "CAPITULO": "chapter",
    "SECAO": "section",
    "SUBSECAO": "subsection",
}
# Applied to accent-stripped text, so "TITULO" also matches the page's "TÍTULO".
_HEADING_RE = re.compile(
    r"^(?P<keyword>PARTE|LIVRO|TITULO|SUBTITULO|CAPITULO|SECAO|SUBSECAO)\s+"
    r"(?P<numeral>[IVXLCDM]+(?:-[A-Z])?|UNIC[AO]|GERAL|ESPECIAL|COMPLEMENTAR)(?=\s|$)",
    re.IGNORECASE,
)
# "Art. 1º", "Art. 3 o", "Art. 1.358-A." and "Art. 759.(Revogado..." (CC).
_ARTICLE_RE = re.compile(
    r"^Art\.\s*(?P<number>\d{1,3}(?:\.\d{3})+|\d+)(?:\s?[º°o](?=[\s.(]))?"
    r"(?:-(?P<suffix>[A-Z]))?\.?(?=[\s(]|$)",
    re.IGNORECASE,
)
_INLINE_NOTE_RE = re.compile(
    r"\((?:reda[cç][aã]o\s+dada|inclu[ií]d[oa]|acrescentad[oa]|revogad[oa]|vide|"
    r"vig[eê]ncia|produ[cç][aã]o\s+de\s+efeito|regulamentad[oa])[^)]*\)|\bVig[eê]ncia\b",
    re.IGNORECASE,
)
_TERMINATOR_RE = re.compile(r"^Bras[ií]lia\s*,", re.IGNORECASE)
_DIVISION_NUMERAL = r"([ivxlcdm]+(?:-[a-z])?|unic[ao]|geral|especial|complementar)\b"

# ... moved verbatim from cdc_snapshot.py: _PARAGRAPH_RE through _REVOKED_RE ...


class StatuteParseError(ValueError):
    """A snapshot does not match its statute specification."""


@dataclass(frozen=True, slots=True)
class ParsedArticle:
    """One top-level article, its hierarchy and its addressable units."""

    number: int
    suffix: str | None
    article_key: str
    article_label: str
    provision_id: str
    part: str | None
    book: str | None
    title: str | None
    subtitle: str | None
    chapter: str | None
    section: str | None
    subsection: str | None
    official_text: str
    status: ProvisionStatus
    units: tuple[LegalTextUnit, ...]


@dataclass(slots=True)
class _Draft:
    number: int
    suffix: str | None
    hierarchy: dict[str, str | None]
    blocks: list[str] = field(default_factory=list)


# ... moved verbatim from cdc_snapshot.py: _ParagraphBuilder, _OfficialHtmlTextParser ...


def extract_paragraphs(html: str) -> list[str]:
    parser = _OfficialHtmlTextParser()
    parser.feed(html)
    parser.close()
    return parser.paragraphs


def parsed_text_sha256(paragraphs: Sequence[str]) -> str:
    """Hash of the extracted text; stable across downloads of the same law."""

    return hashlib.sha256("\n".join(paragraphs).encode("utf-8")).hexdigest()


def match_article_heading(paragraph: str) -> tuple[int, str | None] | None:
    match = _ARTICLE_RE.match(paragraph)
    if match is None:
        return None
    suffix = match.group("suffix")
    return int(match.group("number").replace(".", "")), suffix.upper() if suffix else None


def parse_statute(spec: StatuteSpec, paragraphs: Sequence[str]) -> tuple[ParsedArticle, ...]:
    """Parse and validate every top-level article of one statute."""

    hierarchy: dict[str, str | None] = dict.fromkeys(HIERARCHY_LEVELS)
    drafts: list[_Draft] = []
    current: _Draft | None = None
    open_level: str | None = None
    named = True
    for paragraph in _apply_corrections(spec, paragraphs):
        heading = _HEADING_RE.match(_strip_accents(paragraph))
        if heading:
            level = _LEVEL_BY_KEYWORD[_strip_accents(heading.group("keyword")).upper()]
            _set_level(hierarchy, level, paragraph)
            current, open_level, named = None, level, not _is_bare_heading(paragraph)
            continue
        article = match_article_heading(paragraph)
        if article is not None:
            previous = (drafts[-1].number, drafts[-1].suffix) if drafts else None
            if not _is_next_article(article[0], article[1], previous, spec.known_absent):
                raise StatuteParseError(
                    f"{spec.law_id}: article heading out of sequence after "
                    f"{_describe(previous)}: {paragraph[:80]!r}"
                )
            current = _Draft(article[0], article[1], dict(hierarchy), [paragraph])
            drafts.append(current)
            open_level = None
            continue
        if (
            current is not None
            and current.number == spec.last_article
            and _TERMINATOR_RE.match(paragraph)
        ):
            break
        if open_level is not None:
            # Between a heading and the first article of its division.
            if _EDITORIAL_NOTE_RE.match(paragraph):
                continue
            if _is_uppercase(paragraph) or not named:
                label = hierarchy[open_level]
                hierarchy[open_level] = f"{label} {paragraph}" if label else paragraph
                named = True
                continue
            raise StatuteParseError(
                f"{spec.law_id}: unexpected paragraph after heading "
                f"{hierarchy[open_level]!r}: {paragraph[:80]!r}"
            )
        if current is not None:
            current.blocks.append(paragraph)
    else:
        raise StatuteParseError(f"{spec.law_id}: the signature after the last article is missing")
    articles = tuple(_build_article(spec, draft) for draft in drafts)
    _validate_completeness(spec, articles)
    return articles


def division_numeral(label: str | None, keyword: str) -> str:
    """Extract ``i-a`` from ``TÍTULO I-A (Incluído…) DA EMPRESA…`` for ``titulo``."""

    if not label:
        return ""
    match = re.match(rf"{keyword}\s+{_DIVISION_NUMERAL}", _strip_accents(label).casefold())
    return match.group(1) if match else ""


def in_index_scope(spec: StatuteSpec, article: ParsedArticle) -> bool:
    if spec.index_scope is None:
        return True
    divisions = {
        "parte": division_numeral(article.part, "parte"),
        "livro": division_numeral(article.book, "livro"),
        "titulo": division_numeral(article.title, "titulo"),
        "capitulo": division_numeral(article.chapter, "capitulo"),
    }
    return any(
        all(divisions.get(level) == value for level, value in selector.path)
        for selector in spec.index_scope
    )


def _apply_corrections(spec: StatuteSpec, paragraphs: Sequence[str]) -> list[str]:
    corrected = list(paragraphs)
    for correction in spec.text_corrections:
        positions = [i for i, text in enumerate(corrected) if text.startswith(correction.find)]
        if len(positions) != 1:
            raise StatuteParseError(
                f"{spec.law_id}: correction {correction.find!r} matched "
                f"{len(positions)} paragraphs; expected exactly one"
            )
        index = positions[0]
        corrected[index] = correction.replace + corrected[index][len(correction.find) :]
    return corrected


def _is_next_article(
    number: int,
    suffix: str | None,
    previous: tuple[int, str | None] | None,
    known_absent: Mapping[int, str],
) -> bool:
    if previous is None:
        return number == 1 and suffix is None
    previous_number, previous_suffix = previous
    if suffix is None:
        expected = previous_number + 1
        while expected in known_absent:
            expected += 1
        return number == expected
    if number != previous_number:
        return False
    if previous_suffix is None:
        return suffix == "A"
    return ord(suffix) == ord(previous_suffix) + 1


def _describe(previous: tuple[int, str | None] | None) -> str:
    if previous is None:
        return "the start"
    number, suffix = previous
    return f"art. {number}" if suffix is None else f"art. {number}-{suffix}"


def _set_level(hierarchy: dict[str, str | None], level: str, label: str) -> None:
    index = HIERARCHY_LEVELS.index(level)
    hierarchy[level] = label
    for lower in HIERARCHY_LEVELS[index + 1 :]:
        hierarchy[lower] = None


def _is_bare_heading(paragraph: str) -> bool:
    without_notes = " ".join(_INLINE_NOTE_RE.sub(" ", _strip_accents(paragraph)).split())
    match = _HEADING_RE.match(without_notes)
    return match is not None and match.end() == len(without_notes)


def _is_uppercase(paragraph: str) -> bool:
    letters = [character for character in paragraph if character.isalpha()]
    return bool(letters) and all(character.isupper() for character in letters)


def _build_article(spec: StatuteSpec, draft: _Draft) -> ParsedArticle:
    number, suffix = draft.number, draft.suffix
    article_key = str(number) if suffix is None else f"{number}-{suffix.lower()}"
    label_number = f"{number:,}".replace(",", ".")
    article_label = f"art. {label_number}" if suffix is None else f"art. {label_number}-{suffix}"
    provision_id = f"{spec.law_id}-art-{article_key}"
    units = _build_units(provision_id, draft.blocks)
    statuses = {unit.status for unit in units if unit.kind is not LegalUnitKind.NOTE}
    if statuses == {ProvisionStatus.VETOED}:
        status = ProvisionStatus.VETOED
    elif statuses == {ProvisionStatus.REVOKED}:
        status = ProvisionStatus.REVOKED
    else:
        status = ProvisionStatus.ACTIVE
    return ParsedArticle(
        number=number,
        suffix=suffix,
        article_key=article_key,
        article_label=article_label,
        provision_id=provision_id,
        part=draft.hierarchy["part"],
        book=draft.hierarchy["book"],
        title=draft.hierarchy["title"],
        subtitle=draft.hierarchy["subtitle"],
        chapter=draft.hierarchy["chapter"],
        section=draft.hierarchy["section"],
        subsection=draft.hierarchy["subsection"],
        official_text="\n\n".join(draft.blocks),
        status=status,
        units=units,
    )


def _validate_completeness(spec: StatuteSpec, articles: tuple[ParsedArticle, ...]) -> None:
    # Strict sequencing plus the signature check already prove numbers 1..last
    # minus the declared absences; what remains are suffixes and the total.
    keys = {article.article_key for article in articles}
    if not spec.required_articles <= keys:
        raise StatuteParseError(
            f"{spec.law_id}: missing suffixed articles {sorted(spec.required_articles - keys)}"
        )
    if len(articles) != spec.expected_article_count:
        raise StatuteParseError(
            f"{spec.law_id}: {len(articles)} articles, expected {spec.expected_article_count}"
        )


# ... moved verbatim from cdc_snapshot.py: _build_units, _unstructured_unit_kind,
#     _quoted_unit_hierarchy, _status_for_text, _normalize_text, _strip_accents ...
```

Replace the three `# ... moved verbatim ...` comment lines with the moved code itself; they only mark where it goes. `HTMLParser`, `unicodedata` and `LegalTextUnit` are used by the moved code.

- [ ] **Step 5: Create the package surface**

Create `app/consumer/statutes/__init__.py`:

```python
"""Official statute snapshots: specification, parsing and integrity."""

from app.consumer.statutes.parser import (
    HIERARCHY_LEVELS,
    STATUTE_PARSER_VERSION,
    ParsedArticle,
    StatuteParseError,
    division_numeral,
    extract_paragraphs,
    in_index_scope,
    match_article_heading,
    parse_statute,
    parsed_text_sha256,
)
from app.consumer.statutes.spec import DivisionSelector, StatuteSpec, TextCorrection

__all__ = [
    "HIERARCHY_LEVELS",
    "STATUTE_PARSER_VERSION",
    "DivisionSelector",
    "ParsedArticle",
    "StatuteParseError",
    "StatuteSpec",
    "TextCorrection",
    "division_numeral",
    "extract_paragraphs",
    "in_index_scope",
    "match_article_heading",
    "parse_statute",
    "parsed_text_sha256",
]
```

- [ ] **Step 6: Run the tests to verify they pass**

Run: `python -m pytest tests/test_statute_parser.py -q`
Expected: all pass.

Run: `python -m pytest tests/test_statute_parser.py --cov=app.consumer.statutes --cov-branch --cov-report=term-missing -q`
Expected: 100% for `spec.py` and `parser.py` (`__init__.py` has no branches).

- [ ] **Step 7: Lint, type-check, commit**

Run: `python -m ruff check app tests frontend` and `python -m mypy app`. Expected: clean. If ruff reports C901 for `parse_statute`, add `"app/consumer/statutes/parser.py" = ["C901"]` next to the existing `"app/consumer/cdc_snapshot.py" = ["C901"]` entry in `pyproject.toml`; Task 3 deletes the old entry.

```bash
git add app/consumer/statutes tests/test_statute_parser.py pyproject.toml
git commit -F - <<'EOF'
feat(statutes): add a generic Planalto statute parser driven by specs

Recognizes PARTE/LIVRO/TÍTULO/SUBTÍTULO/CAPÍTULO/SEÇÃO/SUBSEÇÃO, thousands
and suffixed article numbers, declared gaps and exact text corrections, and
fails closed on anything it cannot place.

Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>
Claude-Session: https://claude.ai/code/session_017ENVjKHCXDicafzUtJYJzA
EOF
```

---

### Task 3: Snapshot manifests v3 and the CDC on the generic parser

**Files:**
- Create: `app/consumer/statutes/snapshot.py`, `app/consumer/statutes/registry.py`
- Modify: `app/consumer/statutes/__init__.py` (exports)
- Modify: `app/consumer/data/cdc/manifest.json` (schema 3; the HTML bytes do not change)
- Modify: `app/consumer/legal_corpus.py`
- Delete: `app/consumer/cdc_snapshot.py`, `app/consumer/update_cdc_snapshot.py` (Task 4 adds the generic refresher)
- Modify: `pyproject.toml`, `tests/test_consumer_legal_corpus.py`, `eval_data/consumer_legal_retrieval/dataset.json`
- Test: `tests/test_statute_snapshots.py`

**Interfaces:**
- Consumes: `parse_statute`, `extract_paragraphs`, `parsed_text_sha256`, `STATUTE_PARSER_VERSION`, `StatuteSpec` (Task 2).
- Produces, in `app.consumer.statutes` (re-exported by `__init__`):
  - `MANIFEST_SCHEMA_VERSION = 3`
  - `DATA_DIR: Path` (`app/consumer/data`)
  - `SnapshotManifest` (frozen dataclass) with `from_mapping(value, spec, *, allow_pending=False)` and `to_mapping() -> dict[str, Any]`
  - `LoadedStatute(spec, manifest, articles)`
  - `manifest_path(spec, data_dir=DATA_DIR) -> Path`, `snapshot_path(spec, data_dir=DATA_DIR) -> Path`
  - `load_manifest(spec, data_dir=DATA_DIR, *, allow_pending=False) -> SnapshotManifest`
  - `load_statute(spec, data_dir=DATA_DIR) -> LoadedStatute`
  - `CDC: StatuteSpec`, `STATUTES: tuple[StatuteSpec, ...]` (only the CDC until Task 5)
- Produces, in `app.consumer.legal_corpus`: `_statute_provisions(loaded: LoadedStatute, reviewed: Mapping[str, tuple[str, tuple[str, ...]]] = MappingProxyType({})) -> tuple[LegalProvision, ...]`

- [ ] **Step 1: Write the failing tests**

Create `tests/test_statute_snapshots.py`:

```python
"""Pinned official snapshots: integrity, manifests and CDC identity."""

from __future__ import annotations

import hashlib
import json
import shutil
from collections.abc import Sequence
from pathlib import Path

import pytest

from app.consumer.statutes import (
    CDC,
    STATUTE_PARSER_VERSION,
    ParsedArticle,
    load_manifest,
    load_statute,
    manifest_path,
    snapshot_path,
)

# Computed with the former app/consumer/cdc_snapshot.py parser before its removal.
CDC_LEGACY_DIGEST = "bf4b5660a266627354364867f9b7fbd590f7b679ad2ec21266da03a95c452ccb"


def _legacy_digest(articles: Sequence[ParsedArticle]) -> str:
    payload = [
        {
            "provision_id": article.provision_id,
            "article_key": article.article_key,
            "article_label": article.article_label,
            "title": article.title,
            "chapter": article.chapter,
            "section": article.section,
            "official_text": article.official_text,
            "status": article.status.value,
            "units": [
                {
                    "unit_id": unit.unit_id,
                    "kind": unit.kind.value,
                    "label": unit.label,
                    "text": unit.text,
                    "paragraph": unit.paragraph,
                    "inciso": unit.inciso,
                    "alinea": unit.alinea,
                    "status": unit.status.value,
                }
                for unit in article.units
            ],
        }
        for article in articles
    ]
    canonical = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _copy_cdc(tmp_path: Path) -> Path:
    target = tmp_path / CDC.directory
    target.mkdir()
    shutil.copy(manifest_path(CDC), target / "manifest.json")
    shutil.copy(snapshot_path(CDC), target / CDC.snapshot_file)
    return target


def _rewrite_manifest(directory: Path, **changes: object) -> None:
    path = directory / "manifest.json"
    payload = json.loads(path.read_text(encoding="utf-8"))
    payload.update(changes)
    path.write_text(json.dumps(payload), encoding="utf-8")


def test_cdc_manifest_is_versioned_and_integrity_checked() -> None:
    manifest = load_manifest(CDC)

    assert manifest.schema_version == 3
    assert manifest.release_id == "br-cdc-official-2026-08-04-v1"
    assert manifest.retrieved_on.isoformat() == "2026-08-04"
    assert manifest.parser_version == STATUTE_PARSER_VERSION
    assert manifest.acquisition_method == "download_https"
    assert manifest.http_user_agent is None
    assert manifest.review_status == "engineering_validated"
    assert manifest.parsed_text_sha256 == (
        "ecc3fcb838ad85fb54aaae9b70b7f3cd851cdd66da14018eccaab32af402b5c9"
    )
    assert snapshot_path(CDC).stat().st_size == 169_132
    assert hashlib.sha256(snapshot_path(CDC).read_bytes()).hexdigest() == (
        manifest.snapshot_sha256
    )
    assert manifest.snapshot_sha256 == (
        "bbfa64a79067ad3edd6b4dfff46cf905c85a44b2d5d8b2ac058a6a8855f13ef8"
    )


def test_generic_parser_reproduces_the_cdc_byte_for_byte() -> None:
    articles = load_statute(CDC).articles

    assert len(articles) == 130
    assert _legacy_digest(articles) == CDC_LEGACY_DIGEST
    assert all(
        article.part is None
        and article.book is None
        and article.subtitle is None
        and article.subsection is None
        for article in articles
    )


def test_tampered_snapshot_bytes_are_rejected(tmp_path: Path) -> None:
    directory = _copy_cdc(tmp_path)
    (directory / CDC.snapshot_file).write_bytes(snapshot_path(CDC).read_bytes() + b"tampered")

    with pytest.raises(ValueError, match="integrity check failed"):
        load_statute(CDC, data_dir=tmp_path)


def test_extracted_text_must_match_the_manifest(tmp_path: Path) -> None:
    directory = _copy_cdc(tmp_path)
    _rewrite_manifest(directory, parsed_text_sha256="0" * 64)

    with pytest.raises(ValueError, match="extracted text does not match"):
        load_statute(CDC, data_dir=tmp_path)


def test_manifests_must_match_their_statute_and_be_promoted(tmp_path: Path) -> None:
    directory = _copy_cdc(tmp_path)
    _rewrite_manifest(directory, review_status="pending_review")

    with pytest.raises(ValueError, match="not been promoted"):
        load_manifest(CDC, data_dir=tmp_path)
    assert load_manifest(CDC, data_dir=tmp_path, allow_pending=True).review_status == (
        "pending_review"
    )
    _rewrite_manifest(directory, review_status="engineering_validated", law_id="br-lgpd")
    with pytest.raises(ValueError, match="law_id does not match"):
        load_manifest(CDC, data_dir=tmp_path)


def test_missing_files_and_malformed_manifests_fail_loudly(tmp_path: Path) -> None:
    with pytest.raises(RuntimeError, match="manifest not found"):
        load_manifest(CDC, data_dir=tmp_path)
    directory = _copy_cdc(tmp_path)
    (directory / CDC.snapshot_file).unlink()
    with pytest.raises(RuntimeError, match="snapshot not found"):
        load_statute(CDC, data_dir=tmp_path)
    (directory / "manifest.json").write_text("[]", encoding="utf-8")
    with pytest.raises(ValueError, match="root must be an object"):
        load_manifest(CDC, data_dir=tmp_path)
    (directory / "manifest.json").write_text("{}", encoding="utf-8")
    with pytest.raises(ValueError, match="missing fields"):
        load_manifest(CDC, data_dir=tmp_path)
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `python -m pytest tests/test_statute_snapshots.py -q`
Expected: collection error `ImportError: cannot import name 'CDC' from 'app.consumer.statutes'`.

- [ ] **Step 3: Create `snapshot.py`**

Create `app/consumer/statutes/snapshot.py`:

```python
"""Pinned official snapshots and the manifests that make them auditable.

Production never downloads law: it verifies a local copy. The raw bytes are
pinned by SHA-256, and so is the extracted text, because Planalto's WAF adds a
random script after ``</html>`` to every response and makes raw hashes differ
between two downloads of an unchanged law.
"""

from __future__ import annotations

import dataclasses
import hashlib
import json
import re
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import Any

from app.consumer.statutes.parser import (
    STATUTE_PARSER_VERSION,
    ParsedArticle,
    extract_paragraphs,
    parse_statute,
    parsed_text_sha256,
)
from app.consumer.statutes.spec import StatuteSpec

MANIFEST_SCHEMA_VERSION = 3
DATA_DIR = Path(__file__).resolve().parent.parent / "data"
_SHA256_RE = re.compile(r"[0-9a-f]{64}")
_ACQUISITION_METHODS = frozenset({"download_https", "local_file"})
_REVIEW_STATUSES = frozenset({"pending_review", "engineering_validated", "legal_reviewed"})


@dataclass(frozen=True, slots=True)
class SnapshotManifest:
    """Provenance of one pinned statute snapshot."""

    schema_version: int
    release_id: str
    law_id: str
    source_url: str
    retrieved_on: date
    encoding: str
    snapshot_file: str
    snapshot_sha256: str
    parsed_text_sha256: str
    parser_version: str
    acquisition_method: str
    acquisition_note: str | None
    final_url: str | None
    http_etag: str | None
    http_last_modified: str | None
    http_user_agent: str | None
    refresh_tool_version: str
    review_status: str

    @classmethod
    def from_mapping(
        cls,
        value: dict[str, Any],
        spec: StatuteSpec,
        *,
        allow_pending: bool = False,
    ) -> SnapshotManifest:
        missing = sorted({item.name for item in dataclasses.fields(cls)} - value.keys())
        if missing:
            raise ValueError(f"{spec.law_id} manifest is missing fields: {missing}")

        def optional(name: str) -> str | None:
            item = value[name]
            return None if item is None else str(item)

        manifest = cls(
            schema_version=int(value["schema_version"]),
            release_id=str(value["release_id"]),
            law_id=str(value["law_id"]),
            source_url=str(value["source_url"]),
            retrieved_on=date.fromisoformat(str(value["retrieved_on"])),
            encoding=str(value["encoding"]),
            snapshot_file=str(value["snapshot_file"]),
            snapshot_sha256=str(value["snapshot_sha256"]),
            parsed_text_sha256=str(value["parsed_text_sha256"]),
            parser_version=str(value["parser_version"]),
            acquisition_method=str(value["acquisition_method"]),
            acquisition_note=optional("acquisition_note"),
            final_url=optional("final_url"),
            http_etag=optional("http_etag"),
            http_last_modified=optional("http_last_modified"),
            http_user_agent=optional("http_user_agent"),
            refresh_tool_version=str(value["refresh_tool_version"]),
            review_status=str(value["review_status"]),
        )
        manifest._validate(spec, allow_pending=allow_pending)
        return manifest

    def to_mapping(self) -> dict[str, Any]:
        mapping = dataclasses.asdict(self)
        mapping["retrieved_on"] = self.retrieved_on.isoformat()
        return mapping

    def _validate(self, spec: StatuteSpec, *, allow_pending: bool) -> None:
        problems = (
            (self.schema_version != MANIFEST_SCHEMA_VERSION, "unsupported manifest schema"),
            (self.law_id != spec.law_id, "manifest law_id does not match the statute"),
            (self.source_url != spec.source_url, "manifest does not point to the official URL"),
            (self.snapshot_file != spec.snapshot_file, "manifest names another snapshot file"),
            (self.encoding != spec.encoding, "manifest encoding does not match the statute"),
            (not _SHA256_RE.fullmatch(self.snapshot_sha256), "invalid snapshot SHA-256"),
            (not _SHA256_RE.fullmatch(self.parsed_text_sha256), "invalid parsed-text SHA-256"),
            (self.parser_version != STATUTE_PARSER_VERSION, "parser version does not match runtime"),
            (self.acquisition_method not in _ACQUISITION_METHODS, "invalid acquisition method"),
            (
                self.acquisition_method == "local_file" and not self.acquisition_note,
                "local snapshots require an acquisition note",
            ),
            (self.review_status not in _REVIEW_STATUSES, "invalid review status"),
            (
                self.review_status == "pending_review" and not allow_pending,
                "snapshot has not been promoted after review",
            ),
        )
        for failed, message in problems:
            if failed:
                raise ValueError(f"{spec.law_id}: {message}")


@dataclass(frozen=True, slots=True)
class LoadedStatute:
    """A verified snapshot and its parsed articles."""

    spec: StatuteSpec
    manifest: SnapshotManifest
    articles: tuple[ParsedArticle, ...]


def manifest_path(spec: StatuteSpec, data_dir: Path = DATA_DIR) -> Path:
    return data_dir / spec.directory / "manifest.json"


def snapshot_path(spec: StatuteSpec, data_dir: Path = DATA_DIR) -> Path:
    return data_dir / spec.directory / spec.snapshot_file


def load_manifest(
    spec: StatuteSpec,
    data_dir: Path = DATA_DIR,
    *,
    allow_pending: bool = False,
) -> SnapshotManifest:
    path = manifest_path(spec, data_dir)
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise RuntimeError(f"{spec.law_id} snapshot manifest not found: {path}") from exc
    if not isinstance(raw, dict):
        raise ValueError(f"{spec.law_id} manifest root must be an object")
    return SnapshotManifest.from_mapping(raw, spec, allow_pending=allow_pending)


def load_statute(spec: StatuteSpec, data_dir: Path = DATA_DIR) -> LoadedStatute:
    """Verify the pinned bytes and extracted text, then parse every article."""

    manifest = load_manifest(spec, data_dir)
    path = snapshot_path(spec, data_dir)
    try:
        source = path.read_bytes()
    except FileNotFoundError as exc:
        raise RuntimeError(f"{spec.law_id} snapshot not found: {path}") from exc
    actual = hashlib.sha256(source).hexdigest()
    if actual != manifest.snapshot_sha256:
        raise ValueError(
            f"{spec.law_id} snapshot integrity check failed: "
            f"expected {manifest.snapshot_sha256}, got {actual}"
        )
    paragraphs = extract_paragraphs(source.decode(manifest.encoding))
    if parsed_text_sha256(paragraphs) != manifest.parsed_text_sha256:
        raise ValueError(f"{spec.law_id} extracted text does not match the manifest")
    return LoadedStatute(spec=spec, manifest=manifest, articles=parse_statute(spec, paragraphs))
```

- [ ] **Step 4: Create the registry and export everything**

Create `app/consumer/statutes/registry.py`:

```python
"""The official statutes the Consumer legal corpus can be built from."""

from __future__ import annotations

from app.consumer.schemas import LegalSource
from app.consumer.statutes.spec import StatuteSpec

CDC = StatuteSpec(
    law_id="br-cdc",
    source=LegalSource.CONSUMER_DEFENSE_CODE,
    source_name="Código de Defesa do Consumidor (Lei nº 8.078/1990)",
    citation_prefix="CDC",
    source_url="https://www.planalto.gov.br/ccivil_03/leis/l8078compilado.htm",
    directory="cdc",
    snapshot_file="l8078compilado.html",
    encoding="windows-1252",
    last_article=119,
    expected_article_count=130,
    required_articles=frozenset(
        {"42-a", *(f"54-{suffix}" for suffix in "abcdefg"), *(f"104-{suffix}" for suffix in "abc")}
    ),
)

STATUTES: tuple[StatuteSpec, ...] = (CDC,)
```

In `app/consumer/statutes/__init__.py`, add these imports:

```python
from app.consumer.statutes.registry import CDC, STATUTES
from app.consumer.statutes.snapshot import (
    DATA_DIR,
    MANIFEST_SCHEMA_VERSION,
    LoadedStatute,
    SnapshotManifest,
    load_manifest,
    load_statute,
    manifest_path,
    snapshot_path,
)
```

and add their names to `__all__`: `"CDC"`, `"DATA_DIR"`, `"LoadedStatute"`, `"MANIFEST_SCHEMA_VERSION"`, `"STATUTES"`, `"SnapshotManifest"`, `"load_manifest"`, `"load_statute"`, `"manifest_path"`, `"snapshot_path"`.

- [ ] **Step 5: Migrate the CDC manifest to schema 3**

Replace the whole content of `app/consumer/data/cdc/manifest.json` with the following. The snapshot bytes, their SHA-256 and the acquisition facts are unchanged; `http_user_agent` is `null` because the original download did not record it.

```json
{
  "schema_version": 3,
  "release_id": "br-cdc-official-2026-08-04-v1",
  "law_id": "br-cdc",
  "source_url": "https://www.planalto.gov.br/ccivil_03/leis/l8078compilado.htm",
  "retrieved_on": "2026-08-04",
  "encoding": "windows-1252",
  "snapshot_file": "l8078compilado.html",
  "snapshot_sha256": "bbfa64a79067ad3edd6b4dfff46cf905c85a44b2d5d8b2ac058a6a8855f13ef8",
  "parsed_text_sha256": "ecc3fcb838ad85fb54aaae9b70b7f3cd851cdd66da14018eccaab32af402b5c9",
  "parser_version": "planalto-statute-parser-v1",
  "acquisition_method": "download_https",
  "acquisition_note": null,
  "final_url": "https://www.planalto.gov.br/ccivil_03/leis/l8078compilado.htm",
  "http_etag": null,
  "http_last_modified": null,
  "http_user_agent": null,
  "refresh_tool_version": "cdc-snapshot-refresh-v2",
  "review_status": "engineering_validated"
}
```

- [ ] **Step 6: Run the snapshot tests to verify they pass**

Run: `python -m pytest tests/test_statute_snapshots.py tests/test_statute_parser.py -q`
Expected: all pass. `test_generic_parser_reproduces_the_cdc_byte_for_byte` is the CDC identity guarantee.

- [ ] **Step 7: Build the CDC provisions through the statutes package**

In `app/consumer/legal_corpus.py`:

1. Replace the `from app.consumer.cdc_snapshot import (...)` block with:

```python
from app.consumer.statutes import (
    CDC,
    STATUTE_PARSER_VERSION,
    STATUTES,
    LoadedStatute,
    ParsedArticle,
    SnapshotManifest,
    load_manifest,
    load_statute,
)
```

2. Delete the lines `CDC_URL = ...` and `_SOURCE_NAME_CDC = ...`.
3. Replace the whole function `_cdc_provisions` with:

```python
def _statute_provisions(
    loaded: LoadedStatute,
    reviewed: Mapping[str, tuple[str, tuple[str, ...]]] = MappingProxyType({}),
) -> tuple[LegalProvision, ...]:
    spec, manifest = loaded.spec, loaded.manifest
    provisions: list[LegalProvision] = []
    for article in loaded.articles:
        summary, tags = reviewed.get(article.article_key) or (_caput_extract(article), ())
        provisions.append(
            LegalProvision(
                provision_id=article.provision_id,
                source=spec.source,
                source_name=spec.source_name,
                article=article.article_label,
                citation_label=f"{spec.citation_prefix}, {article.article_label}",
                summary=summary,
                official_url=spec.source_url,
                tags=tuple(tags),
                corpus_release_id=CONSUMER_LAW_CORPUS_RELEASE_ID,
                verified_on=manifest.retrieved_on,
                status=article.status,
                law_id=spec.law_id,
                article_key=article.article_key,
                title=article.title,
                chapter=article.chapter,
                section=article.section,
                official_text=article.official_text,
                source_snapshot_sha256=manifest.snapshot_sha256,
                units=tuple(article.units),
            )
        )
    return tuple(provisions)
```

4. Change the signature `def _caput_extract(article: ParsedCdcArticle) -> str:` to `def _caput_extract(article: ParsedArticle) -> str:`.
5. Replace the `CURATED_PROVISIONS` definition with:

```python
CURATED_PROVISIONS: tuple[LegalProvision, ...] = (
    *_CONSTITUTION_PROVISIONS,
    *_statute_provisions(load_statute(CDC), _REVIEWED_CDC_METADATA),
)
```

6. In `LegalCorpus.__init__`, replace the line `self._validate_cdc_snapshot_provenance()` with:

```python
        law_ids = {provision.law_id for provision in self._provisions}
        self._source_manifests: dict[str, SnapshotManifest] = {
            spec.law_id: load_manifest(spec) for spec in STATUTES if spec.law_id in law_ids
        }
        self._validate_snapshot_provenance()
```

7. Replace the method `_validate_cdc_snapshot_provenance` with:

```python
    def _validate_snapshot_provenance(self) -> None:
        for provision in self._provisions:
            manifest = self._source_manifests.get(provision.law_id or "")
            if manifest is None:
                continue
            if provision.source_snapshot_sha256 != manifest.snapshot_sha256:
                raise ValueError(
                    f"{provision.provision_id} does not match the pinned "
                    f"{provision.law_id} snapshot"
                )
            if provision.official_text is None or provision.official_text_sha256 is None:
                raise ValueError(f"{provision.provision_id} has no integrity-checked official text")
```

8. In `_calculate_corpus_sha256`, replace everything from `cdc_manifest = (` down to the end of the `if cdc_manifest is not None:` block with:

```python
        source_manifests = [
            {**manifest.to_mapping(), "runtime_parser_version": STATUTE_PARSER_VERSION}
            for _, manifest in sorted(self._source_manifests.items())
        ]
```

and in the `payload` dictionary change `"schema_version": 1,` to `"schema_version": 2,`.

- [ ] **Step 8: Remove the CDC-only modules**

```bash
git rm app/consumer/cdc_snapshot.py app/consumer/update_cdc_snapshot.py
```

In `pyproject.toml`:
- delete the line `"app/consumer/cdc_snapshot.py" = ["C901"]`;
- add `"app.consumer.statutes",` to `[tool.coverage.run] source`, keeping alphabetical order.

- [ ] **Step 9: Update the corpus tests**

In `tests/test_consumer_legal_corpus.py`:
1. Replace the `from app.consumer.cdc_snapshot import (...)` block with `from app.consumer.statutes import CDC, load_manifest, load_statute`. Delete the line `from app.consumer.update_cdc_snapshot import refresh_snapshot`.
2. Delete three tests; their replacements live in `tests/test_statute_snapshots.py` (and, for the refresher, Task 4):
   - `test_snapshot_is_offline_versioned_and_integrity_checked`
   - `test_tampered_snapshot_is_rejected`
   - `test_local_snapshot_refresh_requires_provenance_and_explicit_promotion`
3. Replace the body of `test_official_snapshot_covers_complete_compiled_cdc` with:

```python
    loaded = load_statute(CDC)
    ids = {article.provision_id for article in loaded.articles}

    assert loaded.manifest.law_id == "br-cdc"
    assert len(loaded.articles) == 130
    assert {article.number for article in loaded.articles} == set(range(1, 120))
    assert {
        "br-cdc-art-42-a",
        *(f"br-cdc-art-54-{suffix}" for suffix in "abcdefg"),
        *(f"br-cdc-art-104-{suffix}" for suffix in "abc"),
    }.issubset(ids)
    assert "repactuação de dívidas" in next(
        article.official_text
        for article in loaded.articles
        if article.provision_id == "br-cdc-art-104-a"
    )
```

4. Replace every `CDC_SOURCE_URL` with `CDC.source_url`, and every `load_manifest()` with `load_manifest(CDC)`.
5. In `test_corpus_revalidates_copied_models_and_hash_covers_canonical_metadata`, replace
   `monkeypatch.setattr(legal_corpus_module, "CDC_PARSER_VERSION", "cdc-html-parser-audit-test")`
   with
   `monkeypatch.setattr(legal_corpus_module, "STATUTE_PARSER_VERSION", "statute-parser-audit-test")`.
6. Remove imports that became unused (`json`, `date`, `Path`, if nothing else uses them; ruff reports them).
7. Append the guard that makes vector reuse possible:

```python
def test_cdc_and_cf_chunk_texts_are_unchanged() -> None:
    """Pinned before the statute refactor; their vectors are reused (ADR 0017)."""

    texts = sorted(
        chunk.text
        for chunk in get_default_legal_corpus().as_chunks()
        if chunk.metadata["law_id"] in {"br-cf", "br-cdc"}
    )

    assert len(texts) == 472
    assert hashlib.sha256("\x1e".join(texts).encode("utf-8")).hexdigest() == (
        "50d79e182c6a08c5945182f708a64ba3a8d8e09d6ac19f9250462aeeade219ff"
    )
```

- [ ] **Step 10: Re-pin the golden dataset to the new corpus hash**

The corpus hash changed (manifest schema, parser version, hash-payload schema); the release id and every chunk text did not.

Run: `python -c "from app.consumer.legal_corpus import get_default_legal_corpus as g; c = g(); print(c.release_id, c.corpus_sha256)"`
Expected: `br-consumer-law-2026-08-04-v3` followed by a 64-hex hash different from `bef4202f8f74340bc582b00a1901675df4a8da3444d7f0ae22d51aee3917e57c`.
In `eval_data/consumer_legal_retrieval/dataset.json`, set `"target_corpus_sha256"` to the printed hash.

- [ ] **Step 11: Run the full suite and the gates**

Run: `python -m pytest -q`
Expected: all pass. In particular:
- `tests/test_consumer_notice_evaluation.py` still reports 79 / 4 / 0.333, because chunk texts and the document id are unchanged;
- `test_cdc_and_cf_chunk_texts_are_unchanged` passes.

Run: `python -m pytest --cov --cov-report=term-missing -q`
Expected: 100% for `app/consumer/statutes/*`.

Run: `python -m ruff check app tests frontend` and `python -m mypy app`. Expected: clean.

- [ ] **Step 12: Commit**

```bash
git add -A app/consumer tests/test_statute_snapshots.py tests/test_consumer_legal_corpus.py pyproject.toml eval_data/consumer_legal_retrieval/dataset.json
git commit -F - <<'EOF'
refactor(statutes): build the CDC from the generic parser and v3 manifests

The generic parser reproduces every CDC provision and unit byte for byte
(pinned digest) and the 472 CDC/CF chunk texts are unchanged. Manifests now
also pin the extracted-text hash, because Planalto's WAF randomizes raw bytes.

Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>
Claude-Session: https://claude.ai/code/session_017ENVjKHCXDicafzUtJYJzA
EOF
```

---

### Task 4: Generic snapshot refresher

**Files:**
- Create: `app/consumer/update_statute_snapshot.py`
- Test: `tests/test_update_statute_snapshot.py`

**Interfaces:**
- Consumes: `STATUTES`, `DATA_DIR`, `MANIFEST_SCHEMA_VERSION`, `STATUTE_PARSER_VERSION`, `extract_paragraphs`, `parse_statute`, `parsed_text_sha256`, `load_manifest`, `manifest_path`, `snapshot_path` (Tasks 2–3).
- Produces, in `app.consumer.update_statute_snapshot`:
  - `REFRESH_TOOL_VERSION = "statute-snapshot-refresh-v1"`
  - `BROWSER_USER_AGENT: str`
  - `RefreshResult(changed, snapshot_path, manifest_path, article_count, parsed_text_sha256)`
  - `Fetcher = Callable[[str, str], tuple[bytes, str | None, str | None, str | None]]`, returning bytes, final URL, ETag and Last-Modified
  - `refresh_snapshot(spec, retrieved_on, *, data_dir=DATA_DIR, source_file=None, acquisition_note=None, user_agent=BROWSER_USER_AGENT, force=False, fetch=None) -> RefreshResult`
  - `main(argv: list[str] | None = None) -> int`
- CLI: `python -m app.consumer.update_statute_snapshot --law {cdc,…} [--retrieved-on YYYY-MM-DD] [--source-file PATH --acquisition-note TEXT] [--user-agent TEXT] [--data-dir PATH] [--force]`

- [ ] **Step 1: Write the failing tests**

Create `tests/test_update_statute_snapshot.py`:

```python
"""The maintainer refresher: provenance, change detection and review gating."""

from __future__ import annotations

import json
from datetime import date
from pathlib import Path

import pytest

from app.consumer.statutes import CDC, StatuteParseError, load_manifest, snapshot_path
from app.consumer.update_statute_snapshot import (
    BROWSER_USER_AGENT,
    main,
    refresh_snapshot,
)

CDC_TEXT_SHA256 = "ecc3fcb838ad85fb54aaae9b70b7f3cd851cdd66da14018eccaab32af402b5c9"
RETRIEVED_ON = date(2026, 9, 12)


def _cdc_bytes() -> bytes:
    return snapshot_path(CDC).read_bytes()


def _manifest(tmp_path: Path) -> dict[str, object]:
    return json.loads((tmp_path / CDC.directory / "manifest.json").read_text(encoding="utf-8"))


def test_local_refresh_requires_a_note_and_writes_a_pending_manifest(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="acquisition-note"):
        refresh_snapshot(CDC, RETRIEVED_ON, data_dir=tmp_path, source_file=snapshot_path(CDC))

    result = refresh_snapshot(
        CDC,
        RETRIEVED_ON,
        data_dir=tmp_path,
        source_file=snapshot_path(CDC),
        acquisition_note="Cópia baixada do endereço oficial e conferida manualmente.",
    )

    payload = _manifest(tmp_path)
    assert result.changed and result.article_count == 130
    assert payload["release_id"] == "br-cdc-official-2026-09-12-v1"
    assert payload["acquisition_method"] == "local_file"
    assert payload["http_user_agent"] is None
    assert payload["parsed_text_sha256"] == CDC_TEXT_SHA256
    assert payload["review_status"] == "pending_review"
    assert (tmp_path / CDC.directory / CDC.snapshot_file).read_bytes() == _cdc_bytes()
    with pytest.raises(ValueError, match="not been promoted"):
        load_manifest(CDC, data_dir=tmp_path)


def test_unchanged_text_writes_nothing_unless_forced(tmp_path: Path) -> None:
    def fetch(url: str, user_agent: str) -> tuple[bytes, str | None, str | None, str | None]:
        # Planalto's WAF appends a random script after </html> on every response.
        return _cdc_bytes() + b"<script>var token = 'random';</script>", url, None, None

    first = refresh_snapshot(CDC, RETRIEVED_ON, data_dir=tmp_path, fetch=fetch)
    before = (tmp_path / CDC.directory / "manifest.json").read_bytes()
    second = refresh_snapshot(CDC, date(2026, 9, 13), data_dir=tmp_path, fetch=fetch)
    forced = refresh_snapshot(CDC, date(2026, 9, 13), data_dir=tmp_path, fetch=fetch, force=True)

    assert first.changed and not second.changed and forced.changed
    assert second.parsed_text_sha256 == CDC_TEXT_SHA256
    assert before != (tmp_path / CDC.directory / "manifest.json").read_bytes()


def test_download_records_the_user_agent_and_http_metadata(tmp_path: Path) -> None:
    calls: list[tuple[str, str]] = []

    def fetch(url: str, user_agent: str) -> tuple[bytes, str | None, str | None, str | None]:
        calls.append((url, user_agent))
        return _cdc_bytes(), url, '"etag-1"', "Thu, 23 Apr 2026 23:33:33 GMT"

    refresh_snapshot(CDC, RETRIEVED_ON, data_dir=tmp_path, fetch=fetch)

    payload = _manifest(tmp_path)
    assert calls == [(CDC.source_url, BROWSER_USER_AGENT)]
    assert payload["acquisition_method"] == "download_https"
    assert payload["http_user_agent"] == BROWSER_USER_AGENT
    assert payload["http_etag"] == '"etag-1"'
    assert payload["final_url"] == CDC.source_url


def test_empty_and_incomplete_documents_are_rejected(tmp_path: Path) -> None:
    with pytest.raises(RuntimeError, match="empty document"):
        refresh_snapshot(CDC, RETRIEVED_ON, data_dir=tmp_path, fetch=lambda url, ua: (b"", url, None, None))
    truncated = _cdc_bytes()[: len(_cdc_bytes()) // 2]
    with pytest.raises(StatuteParseError):
        refresh_snapshot(
            CDC, RETRIEVED_ON, data_dir=tmp_path, fetch=lambda url, ua: (truncated, url, None, None)
        )
    assert not (tmp_path / CDC.directory / "manifest.json").exists()


def test_cli_refreshes_a_registered_law_into_a_chosen_directory(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    arguments = [
        "--law", "cdc",
        "--source-file", str(snapshot_path(CDC)),
        "--acquisition-note", "Cópia local conferida.",
        "--retrieved-on", "2026-09-12",
        "--data-dir", str(tmp_path),
    ]

    assert main(arguments) == 0
    assert "pending_review" in capsys.readouterr().out
    assert main(arguments) == 0
    assert "Sem alteração textual" in capsys.readouterr().out
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `python -m pytest tests/test_update_statute_snapshot.py -q`
Expected: collection error `ModuleNotFoundError: No module named 'app.consumer.update_statute_snapshot'`.

- [ ] **Step 3: Implement the refresher**

Create `app/consumer/update_statute_snapshot.py`:

```python
"""Maintainer CLI: refresh the pinned snapshot of one official statute.

Runtime code never calls this module. A refresh is an explicit, reviewable
operation: download (or read a local copy), parse and validate completeness,
hash, then atomically replace the snapshot and its manifest. New snapshots are
written as ``pending_review``; promote them to ``engineering_validated`` only
after comparing a sample of articles with the official page.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import tempfile
import urllib.request
from collections.abc import Callable
from dataclasses import dataclass
from datetime import date
from pathlib import Path

from app.consumer.statutes import (
    DATA_DIR,
    MANIFEST_SCHEMA_VERSION,
    STATUTE_PARSER_VERSION,
    STATUTES,
    StatuteSpec,
    extract_paragraphs,
    load_manifest,
    manifest_path,
    parse_statute,
    parsed_text_sha256,
    snapshot_path,
)

REFRESH_TOOL_VERSION = "statute-snapshot-refresh-v1"
# Planalto's WAF resets connections that do not look like a browser. The exact
# string is recorded in the manifest, so the acquisition stays transparent.
BROWSER_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/128.0 Safari/537.36"
)

Fetcher = Callable[[str, str], tuple[bytes, str | None, str | None, str | None]]


@dataclass(frozen=True, slots=True)
class RefreshResult:
    changed: bool
    snapshot_path: Path
    manifest_path: Path
    article_count: int
    parsed_text_sha256: str


def refresh_snapshot(
    spec: StatuteSpec,
    retrieved_on: date,
    *,
    data_dir: Path = DATA_DIR,
    source_file: Path | None = None,
    acquisition_note: str | None = None,
    user_agent: str = BROWSER_USER_AGENT,
    force: bool = False,
    fetch: Fetcher | None = None,
) -> RefreshResult:
    if source_file is None:
        source, final_url, etag, last_modified = (fetch or _download)(spec.source_url, user_agent)
        method, note, agent = "download_https", None, user_agent
    else:
        if not acquisition_note or not acquisition_note.strip():
            raise ValueError("--source-file requires a non-empty --acquisition-note")
        source = source_file.read_bytes()
        final_url = etag = last_modified = None
        method, note, agent = "local_file", acquisition_note.strip(), None
    if not source:
        raise RuntimeError(f"{spec.law_id}: Planalto returned an empty document")

    paragraphs = extract_paragraphs(source.decode(spec.encoding))
    articles = parse_statute(spec, paragraphs)
    text_hash = parsed_text_sha256(paragraphs)
    target_manifest = manifest_path(spec, data_dir)
    target_snapshot = snapshot_path(spec, data_dir)
    if not force and target_manifest.is_file():
        current = load_manifest(spec, data_dir, allow_pending=True)
        if current.parsed_text_sha256 == text_hash:
            return RefreshResult(False, target_snapshot, target_manifest, len(articles), text_hash)

    manifest = {
        "schema_version": MANIFEST_SCHEMA_VERSION,
        "release_id": f"{spec.law_id}-official-{retrieved_on.isoformat()}-v1",
        "law_id": spec.law_id,
        "source_url": spec.source_url,
        "retrieved_on": retrieved_on.isoformat(),
        "encoding": spec.encoding,
        "snapshot_file": spec.snapshot_file,
        "snapshot_sha256": hashlib.sha256(source).hexdigest(),
        "parsed_text_sha256": text_hash,
        "parser_version": STATUTE_PARSER_VERSION,
        "acquisition_method": method,
        "acquisition_note": note,
        "final_url": final_url,
        "http_etag": etag,
        "http_last_modified": last_modified,
        "http_user_agent": agent,
        "refresh_tool_version": REFRESH_TOOL_VERSION,
        "review_status": "pending_review",
    }
    target_snapshot.parent.mkdir(parents=True, exist_ok=True)
    _atomic_write(target_snapshot, source)
    _atomic_write(
        target_manifest,
        (json.dumps(manifest, ensure_ascii=False, indent=2) + "\n").encode("utf-8"),
    )
    return RefreshResult(True, target_snapshot, target_manifest, len(articles), text_hash)


def _download(
    url: str, user_agent: str
) -> tuple[bytes, str | None, str | None, str | None]:  # pragma: no cover - network
    request = urllib.request.Request(url, headers={"User-Agent": user_agent})
    with urllib.request.urlopen(request, timeout=60) as response:  # noqa: S310
        return (
            response.read(),
            response.geturl(),
            response.headers.get("ETag"),
            response.headers.get("Last-Modified"),
        )


def _atomic_write(path: Path, content: bytes) -> None:
    with tempfile.NamedTemporaryFile(dir=path.parent, delete=False) as temporary:
        temporary.write(content)
        temporary.flush()
        temporary_path = Path(temporary.name)
    temporary_path.replace(path)


def main(argv: list[str] | None = None) -> int:
    laws = {spec.law_id.removeprefix("br-"): spec for spec in STATUTES}
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--law", required=True, choices=sorted(laws))
    parser.add_argument("--retrieved-on", type=date.fromisoformat, default=date.today())
    parser.add_argument("--source-file", type=Path, help="pin an already downloaded copy")
    parser.add_argument("--acquisition-note", help="required provenance note for --source-file")
    parser.add_argument("--user-agent", default=BROWSER_USER_AGENT)
    parser.add_argument("--data-dir", type=Path, default=DATA_DIR)
    parser.add_argument(
        "--force", action="store_true", help="write even when the extracted text is unchanged"
    )
    args = parser.parse_args(argv)
    spec = laws[args.law]
    result = refresh_snapshot(
        spec,
        args.retrieved_on,
        data_dir=args.data_dir,
        source_file=args.source_file,
        acquisition_note=args.acquisition_note,
        user_agent=args.user_agent,
        force=args.force,
    )
    if not result.changed:
        print(f"Sem alteração textual em {spec.law_id} ({result.parsed_text_sha256}); nada gravado.")
        return 0
    print(f"{spec.law_id}: {result.article_count} artigos · texto {result.parsed_text_sha256}")
    print(result.snapshot_path)
    print(result.manifest_path)
    print("Manifesto gravado como pending_review: confira artigos por amostragem antes de promover.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `python -m pytest tests/test_update_statute_snapshot.py tests/test_statute_snapshots.py -q`
Expected: all pass. None of these tests touches the network or the real `app/consumer/data` directory.

- [ ] **Step 5: Lint, type-check, commit**

Run: `python -m ruff check app tests frontend` and `python -m mypy app`. Expected: clean.

```bash
git add app/consumer/update_statute_snapshot.py tests/test_update_statute_snapshot.py
git commit -F - <<'EOF'
feat(statutes): add a generic snapshot refresher with change detection

Downloads with a recorded browser user agent (Planalto resets other
clients), validates the parse, skips writing when the extracted text is
unchanged, and always writes new snapshots as pending_review.

Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>
Claude-Session: https://claude.ai/code/session_017ENVjKHCXDicafzUtJYJzA
EOF
```

---

### Task 5: Pin the LGPD and Civil Code snapshots

**Files:**
- Modify: `app/consumer/statutes/registry.py` (add `LGPD` and `CIVIL_CODE`; extend `STATUTES`)
- Modify: `app/consumer/statutes/__init__.py` (export `LGPD`, `CIVIL_CODE`)
- Modify: `.gitattributes`, `pyproject.toml` (`[tool.setuptools.package-data]`)
- Create, with the refresher: `app/consumer/data/lgpd/l13709compilado.html`, `app/consumer/data/lgpd/manifest.json`, `app/consumer/data/cc/l10406compilada.html`, `app/consumer/data/cc/manifest.json`
- Test: `tests/test_statute_snapshots.py` (append)

This task is the only one that contacts `planalto.gov.br`. The default corpus still contains only the CF and the CDC afterwards, so its hash does not change here.

**Interfaces:**
- Produces: `LGPD: StatuteSpec`, `CIVIL_CODE: StatuteSpec`, `STATUTES == (CDC, LGPD, CIVIL_CODE)`.

- [ ] **Step 1: Protect snapshot bytes from line-ending conversion**

Replace the whole content of `.gitattributes` with:

```
app/consumer/data/**/*.html binary
```

In `pyproject.toml`, replace `"app.consumer" = ["data/cdc/*.html", "data/cdc/*.json"]` with:

```toml
"app.consumer" = ["data/*/*.html", "data/*/*.json"]
```

- [ ] **Step 2: Write the failing tests**

Append to `tests/test_statute_snapshots.py`, merging the imports into the existing block:

```python
from collections import Counter

from app.consumer.schemas import ProvisionStatus
from app.consumer.statutes import CIVIL_CODE, LGPD, in_index_scope

LGPD_TEXT_SHA256 = "8232caaef569007b54fa8f38678d5087d2e1f916af6298e2a3654f0416498b27"
CC_TEXT_SHA256 = "cb60b801d600a771f539cf94be7d7990bafb5829924f1e753e4b5224db276802"


def test_lgpd_snapshot_parses_completely() -> None:
    loaded = load_statute(LGPD)
    statuses = Counter(article.status for article in loaded.articles)
    by_key = {article.article_key: article for article in loaded.articles}

    assert loaded.manifest.parsed_text_sha256 == LGPD_TEXT_SHA256
    assert loaded.manifest.review_status == "engineering_validated"
    assert len(loaded.articles) == 80
    assert sum(len(article.units) for article in loaded.articles) == 464
    assert statuses == {
        ProvisionStatus.ACTIVE: 73,
        ProvisionStatus.VETOED: 6,
        ProvisionStatus.REVOKED: 1,
    }
    assert {
        article.provision_id
        for article in loaded.articles
        if article.status is ProvisionStatus.VETOED
    } == {f"br-lgpd-art-{number}" for number in ("28", "55", "56", "57", "58", "59")}
    assert by_key["55-b"].status is ProvisionStatus.REVOKED
    assert (by_key["55-j"].chapter or "").startswith(
        "CAPÍTULO IX DA AGÊNCIA NACIONAL DE PROTEÇÃO DE DADOS"
    )


def test_civil_code_snapshot_parses_completely_and_scopes_the_index() -> None:
    loaded = load_statute(CIVIL_CODE)
    by_key = {article.article_key: article for article in loaded.articles}
    statuses = Counter(article.status for article in loaded.articles)
    in_scope = [article for article in loaded.articles if in_index_scope(CIVIL_CODE, article)]

    assert loaded.manifest.parsed_text_sha256 == CC_TEXT_SHA256
    assert loaded.manifest.review_status == "engineering_validated"
    assert len(loaded.articles) == 2083
    assert sum(len(article.units) for article in loaded.articles) == 3857
    assert statuses == {
        ProvisionStatus.ACTIVE: 2017,
        ProvisionStatus.REVOKED: 65,
        ProvisionStatus.VETOED: 1,
    }
    assert not set(range(1621, 1630)) & {article.number for article in loaded.articles}
    assert by_key["1620"].status is ProvisionStatus.REVOKED
    assert by_key["1636"].status is ProvisionStatus.ACTIVE
    assert by_key["759"].status is ProvisionStatus.REVOKED
    assert by_key["819-a"].status is ProvisionStatus.VETOED
    assert (by_key["1358-a"].provision_id, by_key["1358-a"].article_label) == (
        "br-cc-art-1358-a",
        "art. 1.358-A",
    )
    assert by_key["1"].part == "PARTE GERAL"
    assert by_key["927"].book == "LIVRO I DO DIREITO DAS OBRIGAÇÕES"
    assert (len(in_scope), sum(a.status is ProvisionStatus.ACTIVE for a in in_scope)) == (
        971,
        919,
    )
```

- [ ] **Step 3: Run the tests to verify they fail**

Run: `python -m pytest tests/test_statute_snapshots.py -q -k "lgpd or civil"`
Expected: collection error `ImportError: cannot import name 'CIVIL_CODE'`.

- [ ] **Step 4: Register the two statutes**

In `app/consumer/statutes/registry.py`, change the imports to

```python
from types import MappingProxyType

from app.consumer.schemas import LegalSource
from app.consumer.statutes.spec import DivisionSelector, StatuteSpec, TextCorrection
```

and replace `STATUTES: tuple[StatuteSpec, ...] = (CDC,)` with:

```python
LGPD = StatuteSpec(
    law_id="br-lgpd",
    source=LegalSource.DATA_PROTECTION_LAW,
    source_name="Lei Geral de Proteção de Dados Pessoais (Lei nº 13.709/2018)",
    citation_prefix="LGPD",
    source_url="https://www.planalto.gov.br/ccivil_03/_ato2015-2018/2018/lei/l13709compilado.htm",
    directory="lgpd",
    snapshot_file="l13709compilado.html",
    encoding="windows-1252",
    last_article=65,
    expected_article_count=80,
    required_articles=frozenset(
        {*(f"55-{suffix}" for suffix in "abcdefghijklm"), "58-a", "58-b"}
    ),
    text_corrections=(
        TextCorrection(
            "Art. 5 7. (VETADO).",
            "Art. 57. (VETADO).",
            "espaço espúrio no número do art. 57 na página oficial",
        ),
    ),
)

CIVIL_CODE = StatuteSpec(
    law_id="br-cc",
    source=LegalSource.CIVIL_CODE,
    source_name="Código Civil (Lei nº 10.406/2002)",
    citation_prefix="Código Civil",
    source_url="https://www.planalto.gov.br/ccivil_03/leis/2002/l10406compilada.htm",
    directory="cc",
    snapshot_file="l10406compilada.html",
    encoding="windows-1252",
    last_article=2046,
    expected_article_count=2083,
    required_articles=frozenset(
        {
            "48-a", "49-a", "206-a", "421-a", "819-a", "853-a", "980-a", "1080-a",
            "1240-a", "1354-a", "1487-a", "1775-a", "1783-a", "1815-a",
            *(f"1358-{suffix}" for suffix in "abcdefghijklmnopqrstu"),
            *(f"1368-{suffix}" for suffix in "abcdef"),
            *(f"1510-{suffix}" for suffix in "abcde"),
        }
    ),
    known_absent=MappingProxyType(
        {
            number: "revogados em bloco no parágrafo 'Art. 1.620. a 1.629.' (Lei nº 12.010/2009)"
            for number in range(1621, 1630)
        }
    ),
    text_corrections=(
        TextCorrection("P A R T E G E R A L", "PARTE GERAL", "título com letras espaçadas"),
        TextCorrection("Art 1.636.", "Art. 1.636.", "falta o ponto depois de 'Art'"),
    ),
    # ADR 0016: the Civil Code is subsidiary to the CDC. Only the general part
    # and the law of obligations are indexed; the other books are audit-only.
    index_scope=(
        DivisionSelector((("parte", "geral"),)),
        DivisionSelector((("parte", "especial"), ("livro", "i"))),
    ),
)

STATUTES: tuple[StatuteSpec, ...] = (CDC, LGPD, CIVIL_CODE)
```

In `app/consumer/statutes/__init__.py`, change the registry import to `from app.consumer.statutes.registry import CDC, CIVIL_CODE, LGPD, STATUTES` and add `"CIVIL_CODE"` and `"LGPD"` to `__all__`.

- [ ] **Step 5: Download and pin both snapshots**

Run:

```bash
python -m app.consumer.update_statute_snapshot --law lgpd
python -m app.consumer.update_statute_snapshot --law cc
```

Expected output lines:
- `br-lgpd: 80 artigos · texto 8232caaef569007b54fa8f38678d5087d2e1f916af6298e2a3654f0416498b27`
- `br-cc: 2083 artigos · texto cb60b801d600a771f539cf94be7d7990bafb5829924f1e753e4b5224db276802`

Handle the possible failures as follows:
- **A different text hash:** the law changed after 2026-09-12. Stop and report the difference; do not continue with a new hash.
- **Connection reset:** retry once. If it still fails, save each page from a browser and pin the saved copy:

```bash
python -m app.consumer.update_statute_snapshot --law lgpd --source-file <saved-file> --acquisition-note "Salvo do endereço oficial pelo navegador em <data>; o Planalto recusou o download direto."
```

- [ ] **Step 6: Review a sample and promote**

Print the articles to compare:

```bash
python -c "from app.consumer.statutes import CIVIL_CODE, LGPD, load_manifest; from app.consumer.statutes.snapshot import snapshot_path; from app.consumer.statutes import extract_paragraphs, parse_statute; [print(a.provision_id, a.status.value, '|', a.official_text[:300].replace(chr(10), ' ')) for spec, keys in ((LGPD, {'7', '18', '42', '57', '60'}), (CIVIL_CODE, {'759', '927', '1358-a', '1620', '1636'})) for a in parse_statute(spec, extract_paragraphs(snapshot_path(spec).read_bytes().decode(spec.encoding))) if a.article_key in keys]"
```

Open both official pages in a browser. For each printed article, confirm three things:
- the text matches the page;
- the status is right (LGPD art. 57 vetoed; CC arts. 759 and 1.620 revoked);
- LGPD art. 60 is the Marco Civil amendment.

Then, in `app/consumer/data/lgpd/manifest.json` and `app/consumer/data/cc/manifest.json`, change `"review_status": "pending_review"` to `"review_status": "engineering_validated"`. Record in the pull request description who compared the sample, and when.

- [ ] **Step 7: Run the tests to verify they pass**

Run: `python -m pytest tests/test_statute_snapshots.py tests/test_update_statute_snapshot.py tests/test_consumer_legal_corpus.py -q`
Expected: all pass. The corpus tests are unaffected: the default corpus still holds only the CF and the CDC.

Run: `git check-attr binary app/consumer/data/cc/l10406compilada.html`
Expected: `app/consumer/data/cc/l10406compilada.html: binary: set`.

- [ ] **Step 8: Lint, type-check, commit**

Run: `python -m ruff check app tests frontend` and `python -m mypy app`. Expected: clean.

```bash
git add .gitattributes pyproject.toml app/consumer/statutes app/consumer/data/lgpd app/consumer/data/cc tests/test_statute_snapshots.py
git commit -F - <<'EOF'
feat(statutes): pin the LGPD and Civil Code official snapshots

LGPD: 80 articles (73 active). Civil Code: 2,083 articles; arts. 1.621-1.629
are absent (revoked in bloc), two page quirks are corrected explicitly, and
only the general part and the law of obligations are in the index scope.

Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>
Claude-Session: https://claude.ai/code/session_017ENVjKHCXDicafzUtJYJzA
EOF
```

---

### Task 6: Hierarchy, index scope and corpus performance

**Files:**
- Modify: `app/consumer/schemas.py` (`LegalProvision`, `LegalAuthorityCitation` and its `from_provision`)
- Modify: `app/consumer/statutes/parser.py` and `app/consumer/statutes/__init__.py` (`strip_article_heading`)
- Modify: `app/consumer/legal_corpus.py`
- Modify: `tests/test_consumer_legal_corpus.py`, `tests/test_statute_parser.py`, `eval_data/consumer_legal_retrieval/dataset.json`
- Test: `tests/test_legal_corpus_statutes.py`

**Interfaces:**
- Produces:
  - `LegalProvision` gains `part`, `book`, `subtitle`, `subsection: str | None = None` and `index_scope: Literal["indexed", "audit_only"] = "indexed"`.
  - `LegalAuthorityCitation` gains the same four hierarchy fields, copied by `from_provision`.
  - Chunk metadata gains the keys `part`, `book`, `subtitle`, `subsection`.
  - `LEGAL_CHUNKING_VERSION = "legal-hierarchy-v3"`.
  - `LegalCorpus.document_id: str` (a cached property).
  - `strip_article_heading(text: str) -> str` in `app.consumer.statutes`.
  - `_default_provisions() -> tuple[LegalProvision, ...]`, which replaces the import-time `CURATED_PROVISIONS`.

- [ ] **Step 1: Write the failing tests**

Create `tests/test_legal_corpus_statutes.py`:

```python
"""Hierarchy, index scope and performance of a corpus built from statutes."""

from __future__ import annotations

from functools import lru_cache

import pytest

from app.consumer.legal_corpus import (
    LegalCorpus,
    _statute_provisions,
    get_default_legal_corpus,
)
from app.consumer.statutes import CIVIL_CODE, load_statute
from app.schemas.document import ParsedDocument
from app.schemas.rag import RetrievedChunk


@lru_cache(maxsize=1)
def _with_civil_code() -> LegalCorpus:
    base = get_default_legal_corpus().provisions
    return LegalCorpus((*base, *_statute_provisions(load_statute(CIVIL_CODE))))


def test_hierarchy_reaches_provisions_chunks_and_citations() -> None:
    corpus = _with_civil_code()
    chunk = next(
        item
        for item in corpus.as_chunks()
        if item.metadata["unit_id"] == "br-cc-art-927-caput"
    )

    citation = corpus.authority_for_chunk(RetrievedChunk(chunk=chunk, score=1.0))

    assert corpus.get("br-cc-art-927").book == "LIVRO I DO DIREITO DAS OBRIGAÇÕES"
    assert chunk.text.startswith(
        "Código Civil (Lei nº 10.406/2002) > PARTE ESPECIAL > "
        "LIVRO I DO DIREITO DAS OBRIGAÇÕES > TÍTULO IX"
    )
    assert (chunk.metadata["part"], chunk.metadata["book"]) == (
        "PARTE ESPECIAL",
        "LIVRO I DO DIREITO DAS OBRIGAÇÕES",
    )
    assert citation.citation_label == "Código Civil, art. 927"
    assert citation.book == "LIVRO I DO DIREITO DAS OBRIGAÇÕES"


def test_audit_only_books_stay_in_the_corpus_but_not_in_the_index() -> None:
    corpus = _with_civil_code()
    family = corpus.get("br-cc-art-1511")
    indexed = {chunk.metadata["provision_id"] for chunk in corpus.as_chunks()}
    retrievable = {item.provision_id for item in corpus.retrievable_provisions()}

    assert family.index_scope == "audit_only"
    assert corpus.get("br-cc-art-927").index_scope == "indexed"
    assert "br-cc-art-1511" not in indexed | retrievable
    assert "br-cc-art-927" in indexed & retrievable


def test_civil_code_summaries_drop_the_article_heading() -> None:
    corpus = _with_civil_code()

    assert corpus.get("br-cc-art-1358-a").summary.startswith("Pode haver, em terrenos")
    assert corpus.get("br-cc-art-3").summary.startswith("São absolutamente incapazes")


def test_the_document_id_is_hashed_once(monkeypatch: pytest.MonkeyPatch) -> None:
    calls = {"count": 0}
    original = ParsedDocument.doc_id

    def counting(self: ParsedDocument) -> str:
        calls["count"] += 1
        return original.fget(self)  # type: ignore[attr-defined, no-any-return]

    monkeypatch.setattr(ParsedDocument, "doc_id", property(counting))
    corpus = LegalCorpus(get_default_legal_corpus().provisions)

    corpus.as_chunks()
    corpus.as_chunks()

    assert calls["count"] == 1
```

`"Pode haver, em terrenos"` is the start of CC art. 1.358-A (condomínio de lotes). If Step 7 shows another opening, compare it with the official page before adjusting the assertion.

Append to `tests/test_statute_parser.py` (add `strip_article_heading` to its import block):

```python
def test_strip_article_heading_keeps_the_rule_text() -> None:
    assert strip_article_heading("Art. 1.358-A. Pode haver") == "Pode haver"
    assert strip_article_heading("Art. 3 o São absolutamente") == "São absolutamente"
    assert strip_article_heading("Texto sem cabeçalho") == "Texto sem cabeçalho"
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `python -m pytest tests/test_legal_corpus_statutes.py tests/test_statute_parser.py -q`
Expected: FAIL. `strip_article_heading` cannot be imported, and `LegalProvision` has no attribute `book`.

- [ ] **Step 3: Extend the schemas**

In `app/consumer/schemas.py`:
1. Add `from typing import Literal` to the imports.
2. In `LegalProvision`, add after `section: str | None = None`:

```python
    part: str | None = None
    book: str | None = None
    subtitle: str | None = None
    subsection: str | None = None
    index_scope: Literal["indexed", "audit_only"] = "indexed"
```

3. In `LegalAuthorityCitation`, add after `section: str | None = None`:

```python
    part: str | None = None
    book: str | None = None
    subtitle: str | None = None
    subsection: str | None = None
```

4. In `LegalAuthorityCitation.from_provision`, add to the `cls(...)` call after `section=provision.section,`:

```python
            part=provision.part,
            book=provision.book,
            subtitle=provision.subtitle,
            subsection=provision.subsection,
```

- [ ] **Step 4: Add `strip_article_heading`**

In `app/consumer/statutes/parser.py`, add after `match_article_heading`:

```python
def strip_article_heading(text: str) -> str:
    """The text of a caput without its ``Art. N`` heading."""

    match = _ARTICLE_RE.match(text)
    return text[match.end() :].lstrip() if match else text
```

and export it from `app/consumer/statutes/__init__.py` (import and `__all__`).

- [ ] **Step 5: Carry hierarchy and scope through the corpus**

In `app/consumer/legal_corpus.py`:

1. Change `LEGAL_CHUNKING_VERSION = "legal-hierarchy-v2"` to `LEGAL_CHUNKING_VERSION = "legal-hierarchy-v3"`.
2. Add `in_index_scope` and `strip_article_heading` to the `from app.consumer.statutes import (...)` block.
3. In `_statute_provisions`, add these arguments to the `LegalProvision(...)` call, after `section=article.section,`:

```python
                part=article.part,
                book=article.book,
                subtitle=article.subtitle,
                subsection=article.subsection,
                index_scope="indexed" if in_index_scope(spec, article) else "audit_only",
```

4. Replace the first two lines of `_caput_extract`'s body (`caput = article.units[0].text` and the `re.sub` line) with:

```python
    caput = strip_article_heading(article.units[0].text)
```

5. Replace the `CURATED_PROVISIONS` definition with a function, and build the default corpus from it:

```python
def _default_provisions() -> tuple[LegalProvision, ...]:
    return (
        *_CONSTITUTION_PROVISIONS,
        *_statute_provisions(load_statute(CDC), _REVIEWED_CDC_METADATA),
    )
```

and in `get_default_legal_corpus`, replace `return LegalCorpus(CURATED_PROVISIONS)` with `return LegalCorpus(_default_provisions())`.
6. In `LegalCorpus.__init__`, add `self._document_id: str | None = None` next to `self._parsed_document: ParsedDocument | None = None`. After the `corpus_sha256` property, add:

```python
    @property
    def document_id(self) -> str:
        """The corpus document id, hashed once.

        ``ParsedDocument.doc_id`` joins and hashes the whole corpus text on
        every access; chunk generation used to read it twice per chunk.
        """

        if self._document_id is None:
            self._document_id = self.as_parsed_document().doc_id
        return self._document_id
```

7. In `as_chunks`:
   - replace `document = self.as_parsed_document()` with `document_id = self.document_id`;
   - replace the two uses of `document.doc_id` inside the loop with `document_id`;
   - replace `document_id=document.doc_id,` in the `_article_level_chunks(...)` call with `document_id=document_id,`;
   - replace the skip condition `if _is_amendment_only(provision):` with `if _is_amendment_only(provision) or provision.index_scope != "indexed":`.
8. In `retrievable_provisions`, replace the filter `if not _is_amendment_only(provision)` with `if not _is_amendment_only(provision) and provision.index_scope == "indexed"`.
9. In `provisions_for_chunk`, replace
   `document = self.as_parsed_document()` and `if chunk.doc_id != document.doc_id:`
   with the single condition `if chunk.doc_id != self.document_id:`.
10. In `_legal_metadata`, add after `"section": provision.section,`:

```python
            "part": provision.part,
            "book": provision.book,
            "subtitle": provision.subtitle,
            "subsection": provision.subsection,
```

11. In `_calculate_corpus_sha256`, replace the `"hierarchy": {...}` entry of each provision with the following, and add `"index_scope"` next to it:

```python
                    "hierarchy": {
                        "part": provision.part,
                        "book": provision.book,
                        "title": provision.title,
                        "subtitle": provision.subtitle,
                        "chapter": provision.chapter,
                        "section": provision.section,
                        "subsection": provision.subsection,
                    },
                    "index_scope": provision.index_scope,
```

12. In `_page_text`, replace the tuple `(provision.title, provision.chapter, provision.section)` with `(provision.part, provision.book, provision.title, provision.subtitle, provision.chapter, provision.section, provision.subsection)`.
13. In `_section_label`, replace the tuple with:

```python
            for item in (
                provision.source_name,
                provision.part,
                provision.book,
                provision.title,
                provision.subtitle,
                provision.chapter,
                provision.section,
                provision.subsection,
                provision.article,
            )
```

For CF and CDC provisions every new level is `None`, so their page texts, chunk headers and chunk texts are unchanged.

- [ ] **Step 6: Update the existing corpus tests**

In `tests/test_consumer_legal_corpus.py`:
1. In `test_legal_aware_chunks_never_cross_articles_and_expose_metadata`, change `"chunking_version": "legal-hierarchy-v2:target=1200",` to `"chunking_version": "legal-hierarchy-v3:target=1200",` and add these keys to the expected dictionary:

```python
        "part": None,
        "book": None,
        "subtitle": None,
        "subsection": None,
```

2. In `test_corpus_revalidates_copied_models_and_hash_covers_canonical_metadata`, after the `changed_hierarchy` assertion, add:

```python
    changed_scope = provision.model_copy(update={"index_scope": "audit_only"})
    assert LegalCorpus([changed_scope]).corpus_sha256 != baseline.corpus_sha256
```

- [ ] **Step 7: Run the tests to verify they pass**

Run: `python -m pytest tests/test_legal_corpus_statutes.py tests/test_statute_parser.py tests/test_consumer_legal_corpus.py -q`
Expected: all pass, including `test_cdc_and_cf_chunk_texts_are_unchanged`.

- [ ] **Step 8: Re-pin the golden dataset**

Run: `python -c "from app.consumer.legal_corpus import get_default_legal_corpus as g; c = g(); print(c.release_id, c.corpus_sha256)"`
Set `"target_corpus_sha256"` in `eval_data/consumer_legal_retrieval/dataset.json` to the printed hash. The release id is still `br-consumer-law-2026-08-04-v3`.

- [ ] **Step 9: Full suite, lint, type-check, commit**

Run: `python -m pytest -q`
Expected: all pass. The notice baseline is still 79 / 4 / 0.333.
Run: `python -m ruff check app tests frontend` and `python -m mypy app`. Expected: clean.

```bash
git add app/consumer/schemas.py app/consumer/statutes app/consumer/legal_corpus.py tests/test_legal_corpus_statutes.py tests/test_consumer_legal_corpus.py tests/test_statute_parser.py eval_data/consumer_legal_retrieval/dataset.json
git commit -F - <<'EOF'
feat(consumer): carry statute hierarchy and index scope through the corpus

Adds part/book/subtitle/subsection to provisions, citations, chunk metadata
and the corpus hash; audit-only divisions are kept but never chunked. The
corpus hashes its document id once (as_chunks was quadratic in corpus size)
and builds the default provisions lazily.

Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>
Claude-Session: https://claude.ai/code/session_017ENVjKHCXDicafzUtJYJzA
EOF
```

---

### Task 7: Eligibility, CDC precedence and retrieval vocabulary

**Files:**
- Modify: `app/consumer/legal_policy.py`
- Modify: `app/consumer/ground_selection.py` (the final loop of `select_legal_grounds`)
- Modify: `app/consumer/retrieval.py` (`_CATEGORY_EXPANSIONS`, `_SUBCATEGORY_SIGNALS`)
- Modify: `app/evaluation/consumer_runner.py` (`QUERY_BUILDER_VERSION`)
- Modify: `tests/test_consumer_api.py:170`, `tests/test_consumer_evaluation.py:274`, `tests/test_consumer_notice_evaluation.py` (only if the baseline moves; see Step 8)
- Test: `tests/test_legal_policy_sources.py` (new), `tests/test_ground_selection.py` (append), `tests/test_consumer_retrieval.py` (append)

**Interfaces:**
- Produces, in `app.consumer.legal_policy`:
  - `LEGAL_GROUND_POLICY_VERSION = "consumer-notice-scope-eligibility-v3"`
  - `COMPLEMENTARY_SOURCES = frozenset({LegalSource.DATA_PROTECTION_LAW, LegalSource.CIVIL_CODE})`
  - `MAX_COMPLEMENTARY_GROUNDS = 3`
  - `precedence_window(candidates: Sequence[tuple[LegalProvision, T]], *, window: int, lexical_only: bool) -> list[tuple[LegalProvision, T]]`
  - `enforce_cdc_anchor(selected: Sequence[tuple[LegalProvision, T]]) -> list[tuple[LegalProvision, T]]`
- Produces: retrieval subcategory `personal_data`; `QUERY_BUILDER_VERSION = "consumer-legal-three-query-v4"`.

- [ ] **Step 1: Write the failing tests**

Create `tests/test_legal_policy_sources.py`:

```python
"""Eligibility and precedence for the LGPD and the Civil Code."""

from __future__ import annotations

from datetime import date

from app.consumer.legal_policy import (
    MAX_COMPLEMENTARY_GROUNDS,
    enforce_cdc_anchor,
    precedence_window,
    provision_is_eligible,
)
from app.consumer.schemas import LAW_ID_BY_SOURCE, LegalProvision, LegalSource


def _provision(source: LegalSource, number: int, **overrides: object) -> LegalProvision:
    payload: dict[str, object] = {
        "provision_id": f"{LAW_ID_BY_SOURCE[source]}-art-{number}",
        "source": source,
        "source_name": "Fonte de teste",
        "article": f"art. {number}",
        "citation_label": f"Teste, art. {number}",
        "summary": "Resumo de teste.",
        "official_url": "https://www.planalto.gov.br/teste.htm",
        "corpus_release_id": "test",
        "verified_on": date(2026, 9, 12),
        "title": "TÍTULO I Dos Direitos do Consumidor",
    }
    payload.update(overrides)
    return LegalProvision.model_validate(payload)


def _lgpd(chapter: str | None) -> LegalProvision:
    return _provision(LegalSource.DATA_PROTECTION_LAW, 18, chapter=chapter, title=None)


def test_lgpd_chapters_a_notice_cannot_rest_on() -> None:
    assert provision_is_eligible(_lgpd("CAPÍTULO III DOS DIREITOS DO TITULAR"))
    assert provision_is_eligible(_lgpd("CAPÍTULO VI DOS AGENTES DE TRATAMENTO DE DADOS PESSOAIS"))
    for excluded in (
        "CAPÍTULO IV DO TRATAMENTO DE DADOS PESSOAIS PELO PODER PÚBLICO",
        "CAPÍTULO VIII DA FISCALIZAÇÃO",
        "CAPÍTULO IX DA AGÊNCIA NACIONAL DE PROTEÇÃO DE DADOS",
        "CAPÍTULO X DISPOSIÇÕES FINAIS E TRANSITÓRIAS",
        None,
    ):
        assert not provision_is_eligible(_lgpd(excluded)), excluded


def test_civil_code_and_audit_only_provisions() -> None:
    assert provision_is_eligible(_provision(LegalSource.CIVIL_CODE, 927))
    assert not provision_is_eligible(
        _provision(LegalSource.CIVIL_CODE, 1511, index_scope="audit_only")
    )
    assert not provision_is_eligible(
        _provision(LegalSource.CONSUMER_DEFENSE_CODE, 42, index_scope="audit_only")
    )


def test_the_window_caps_complementary_sources_without_losing_slots() -> None:
    civil = [(_provision(LegalSource.CIVIL_CODE, n), f"cc-{n}") for n in range(1, 6)]
    cdc = [(_provision(LegalSource.CONSUMER_DEFENSE_CODE, n), f"cdc-{n}") for n in range(1, 7)]

    admitted = precedence_window([*civil, *cdc], window=8, lexical_only=False)

    assert MAX_COMPLEMENTARY_GROUNDS == 3
    assert [item for _, item in admitted] == [
        "cc-1", "cc-2", "cc-3", "cdc-1", "cdc-2", "cdc-3", "cdc-4", "cdc-5",
    ]


def test_lexical_only_retrieval_admits_no_complementary_source() -> None:
    mixed = [
        (_lgpd("CAPÍTULO VI DOS AGENTES DE TRATAMENTO DE DADOS PESSOAIS"), "lgpd"),
        (_provision(LegalSource.CONSUMER_DEFENSE_CODE, 14), "cdc"),
    ]

    assert [item for _, item in precedence_window(mixed, window=8, lexical_only=True)] == ["cdc"]


def test_complementary_grounds_need_a_cdc_anchor() -> None:
    cdc = (_provision(LegalSource.CONSUMER_DEFENSE_CODE, 14), "cdc")
    cf = (_provision(LegalSource.FEDERAL_CONSTITUTION, 5), "cf")
    civil = (_provision(LegalSource.CIVIL_CODE, 927), "cc")

    assert [item for _, item in enforce_cdc_anchor([civil, cdc])] == ["cc", "cdc"]
    assert [item for _, item in enforce_cdc_anchor([civil, cf])] == ["cf"]
```

Append to `tests/test_ground_selection.py`, merging the imports:

```python
from functools import lru_cache

from app.consumer.legal_corpus import LegalCorpus, _statute_provisions
from app.consumer.statutes import CIVIL_CODE, LGPD, load_statute


@lru_cache(maxsize=1)
def _mixed_corpus() -> LegalCorpus:
    return LegalCorpus(
        (
            *get_default_legal_corpus().provisions,
            *_statute_provisions(load_statute(LGPD)),
            *_statute_provisions(load_statute(CIVIL_CODE)),
        )
    )


def _mixed_chunk(unit_id: str) -> Chunk:
    return next(
        chunk for chunk in _mixed_corpus().as_chunks() if chunk.metadata["unit_id"] == unit_id
    )


def test_complementary_grounds_are_capped_and_keep_cdc_in_the_window() -> None:
    complementary = [
        _mixed_chunk(unit_id)
        for unit_id in (
            "br-cc-art-876-caput",
            "br-cc-art-884-caput",
            "br-cc-art-927-caput",
            "br-cc-art-944-caput",
            "br-lgpd-art-42-caput",
        )
    ]
    ranked = [*complementary, _mixed_chunk("br-cdc-art-42-paragrafo-unico")]
    results = [
        [RetrievedChunk(chunk=chunk, score=0.03 - index * 0.0001) for index, chunk in enumerate(ranked)]
    ]

    grounds = select_legal_grounds(
        _mixed_corpus(), _facts(), results, [_trace(results[0])], support=_everything_supported
    )

    law_ids = [ground.authority.law_id for ground in grounds]
    assert law_ids.count("br-cdc") == 1
    assert sum(law_id in {"br-cc", "br-lgpd"} for law_id in law_ids) == 3


def test_without_a_cdc_ground_complementary_sources_are_dropped() -> None:
    results = [[RetrievedChunk(chunk=_mixed_chunk("br-cc-art-876-caput"), score=0.03)]]

    grounds = select_legal_grounds(
        _mixed_corpus(), _facts(), results, [_trace(results[0])], support=_everything_supported
    )

    assert grounds == []


def test_degraded_retrieval_cites_no_complementary_source() -> None:
    results = [
        [
            RetrievedChunk(chunk=_mixed_chunk("br-cc-art-876-caput"), score=0.03),
            RetrievedChunk(chunk=_mixed_chunk("br-cdc-art-42-paragrafo-unico"), score=0.029),
        ]
    ]
    degraded = _trace(results[0]).model_copy(update={"degraded_mode": "lexical_only"})

    grounds = select_legal_grounds(
        _mixed_corpus(), _facts(), results, [degraded], support=_everything_supported
    )

    assert [ground.authority.law_id for ground in grounds] == ["br-cdc"]
```

Append to `tests/test_consumer_retrieval.py`:

```python
def test_personal_data_complaints_get_data_protection_vocabulary() -> None:
    facts = _facts(
        issue_category=ConsumerIssueCategory.OTHER,
        complaint_summary="A loja teve um vazamento de dados e usaram meu CPF num crediário.",
        desired_resolution="Quero saber quais dados vazaram.",
    )

    queries = build_legal_queries(facts)

    assert infer_retrieval_category("other", "Vazaram meus dados pessoais") == "personal_data"
    assert "tratamento de dados pessoais" in queries[2]
    assert "segurança do serviço" in queries[2]
    assert not any(character.isdigit() for character in queries[2])


def test_specific_consumer_signals_still_win_over_personal_data() -> None:
    assert (
        infer_retrieval_category("other", "Houve venda casada e usaram meus dados pessoais.")
        == "abusive_practice"
    )


def test_numeric_article_anchors_name_the_cdc() -> None:
    assert "CDC artigo 42" in build_legal_queries(_facts())[2]
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `python -m pytest tests/test_legal_policy_sources.py tests/test_ground_selection.py tests/test_consumer_retrieval.py -q`
Expected: FAIL. The new names cannot be imported, the LGPD chapters are all eligible, and the queries still say `artigo 42` without `CDC`.

- [ ] **Step 3: Extend the policy**

In `app/consumer/legal_policy.py`:
1. Add `from collections.abc import Sequence` and `from typing import TypeVar` to the imports.
2. Change `LEGAL_GROUND_POLICY_VERSION` to `"consumer-notice-scope-eligibility-v3"`.
3. After `_EXCLUDED_CDC_DIVISIONS`, add:

```python
# ADR 0016: the LGPD and the Civil Code complement the CDC in a consumer
# notice; they never ground one on their own.
COMPLEMENTARY_SOURCES = frozenset({LegalSource.DATA_PROTECTION_LAW, LegalSource.CIVIL_CODE})
MAX_COMPLEMENTARY_GROUNDS = 3
# LGPD chapters an individual notice to a supplier cannot rest on: processing
# by public bodies (IV), administrative sanctions (VIII), the national
# authority and council (IX), and final and transitional provisions (X).
_EXCLUDED_LGPD_CHAPTERS = frozenset({"iv", "viii", "ix", "x"})

_Candidate = TypeVar("_Candidate")
```

4. Replace the first `if` of `provision_is_eligible` (the block starting `if provision.source is not LegalSource.CONSUMER_DEFENSE_CODE:`) with:

```python
    if provision.index_scope != "indexed":
        return False
    if provision.source is LegalSource.FEDERAL_CONSTITUTION:
        # The constitutional corpus is a small, hand-reviewed selection of
        # consumer-relevant provisions; every entry is already in scope.
        return True
    if provision.source is LegalSource.CIVIL_CODE:
        # The index scope already limits the Civil Code to the general part
        # and the law of obligations.
        return True
    if provision.source is LegalSource.DATA_PROTECTION_LAW:
        chapter = _division_numeral(provision.chapter, "capitulo")
        return bool(chapter) and chapter not in _EXCLUDED_LGPD_CHAPTERS
```

5. Add these two functions after `eligible_provisions`:

```python
def precedence_window(
    candidates: Sequence[tuple[LegalProvision, _Candidate]],
    *,
    window: int,
    lexical_only: bool,
) -> list[tuple[LegalProvision, _Candidate]]:
    """Admit rank-ordered candidates into the ground window.

    Complementary sources take at most MAX_COMPLEMENTARY_GROUNDS slots; extra
    ones are skipped without consuming a slot, so the Civil Code cannot push
    CDC articles out of the window. Without semantic retrieval they are not
    admitted at all.
    """

    admitted: list[tuple[LegalProvision, _Candidate]] = []
    complementary = 0
    for provision, candidate in candidates:
        if provision.source in COMPLEMENTARY_SOURCES:
            if lexical_only or complementary >= MAX_COMPLEMENTARY_GROUNDS:
                continue
            complementary += 1
        admitted.append((provision, candidate))
        if len(admitted) == window:
            break
    return admitted


def enforce_cdc_anchor(
    selected: Sequence[tuple[LegalProvision, _Candidate]],
) -> list[tuple[LegalProvision, _Candidate]]:
    """Complementary grounds stand only beside at least one CDC ground."""

    if any(provision.source is LegalSource.CONSUMER_DEFENSE_CODE for provision, _ in selected):
        return list(selected)
    return [
        (provision, candidate)
        for provision, candidate in selected
        if provision.source not in COMPLEMENTARY_SOURCES
    ]
```

6. Add a paragraph to the module docstring, before `Eligibility is still not a merits decision`:

```text
The LGPD and the Civil Code follow the same idea (ADR 0016). LGPD chapters
on public bodies, administrative sanctions, the national authority and final
provisions are excluded; the Civil Code is limited by its index scope. Both are
complementary: at most three of their grounds, only beside a CDC ground, and
none when retrieval fell back to lexical-only search.
```

- [ ] **Step 4: Apply the precedence in the selector**

In `app/consumer/ground_selection.py`:
1. Extend the policy import to `from app.consumer.legal_policy import (enforce_cdc_anchor, precedence_window, provision_is_eligible, strongly_supported_chunk_ids)`, and add `LegalProvision` to the `app.consumer.schemas` import.
2. Replace everything from `grounds: list[LegalGround] = []` to the end of `select_legal_grounds` with:

```python
    issue = issue_label(facts)
    provision_candidates.sort(key=lambda item: (item[0], item[3].chunk.chunk_id))
    lexical_only = any(trace.degraded_mode == "lexical_only" for trace in traces or [])
    window = precedence_window(
        [(corpus.provision_for_chunk(item[3]), item) for item in provision_candidates],
        window=MAX_GROUND_CANDIDATES,
        lexical_only=lexical_only,
    )
    selected: list[tuple[LegalProvision, LegalGround]] = []
    for provision, (_, provision_score, rank, result) in window:
        if provision_score < score_floor:
            continue
        authority = corpus.authority_for_chunk(result, retrieval_rank=rank)
        selected.append(
            (
                provision,
                LegalGround(
                    authority=authority,
                    application_to_facts=(
                        f"O texto oficial em {provision.citation_label} foi localizado "
                        f"pela política de recuperação para {issue}. Sua aplicabilidade "
                        "ao caso não foi decidida pelo sistema e deve ser validada por "
                        "profissional habilitado contra os fatos e documentos citados."
                    ),
                ),
            )
        )
        if len(selected) >= MAX_LEGAL_GROUNDS:
            break
    return [ground for _, ground in enforce_cdc_anchor(selected)]
```

With CDC and CF candidates only, this returns exactly what the previous loop returned.

- [ ] **Step 5: Retrieval vocabulary**

In `app/consumer/retrieval.py`, set these entries of `_CATEGORY_EXPANSIONS`. Leave the entries not listed unchanged.

```python
    "unauthorized_charge": (
        "cobrança indevida repetição do indébito pagamento em excesso CDC artigo 42"
    ),
    "negative_credit_record": (
        "cadastro de consumidores negativação correção de dados cobrança CDC artigo 43"
    ),
    "service_failure": (
        "vício de produto ou serviço qualidade adequação reparação CDC artigos 18 20"
    ),
    "product_defect": "vício do produto substituição restituição abatimento CDC artigo 18",
    "non_delivery": "oferta descumprida entrega forçada restituição CDC artigo 35",
    "right_of_withdrawal": (
        "direito de arrependimento contratação fora do estabelecimento CDC artigo 49"
    ),
    "misleading_advertising": (
        "publicidade enganosa ou abusiva oferta informação CDC artigos 36 37 38"
    ),
    "abusive_practice": "prática abusiva vantagem manifestamente excessiva CDC artigo 39",
    "abusive_collection": "cobrança de dívida ameaça constrangimento exposição CDC artigo 42",
    "public_utility": "serviço público adequado eficiente seguro contínuo CDC artigo 22",
    "over_indebtedness": (
        "superendividamento crédito responsável repactuação conciliação CDC artigos 54-A 104-A"
    ),
    # No article numbers: "42" and "43" now also name LGPD and Civil Code
    # articles. The CDC terms let the CDC anchor required by ADR 0016 surface.
    "personal_data": (
        "tratamento de dados pessoais direitos do titular acesso correção eliminação "
        "consentimento compartilhamento finalidade segurança do serviço incidente de "
        "segurança vazamento informação responsabilidade reparação de danos cadastros "
        "e dados do consumidor"
    ),
```

Append this entry as the last item of `_SUBCATEGORY_SIGNALS`, so the more specific consumer subcategories keep priority:

```python
    (
        "personal_data",
        (
            "vazamento de dados",
            "vazaram meus dados",
            "vazamento dos meus dados",
            "dados vazados",
            "compartilharam meus dados",
            "compartilhou meus dados",
            "repassou meus dados",
            "repassou meu número",
            "venderam meus dados",
            "vendeu meus dados",
            "excluir meus dados",
            "apagar meus dados",
            "exclusão dos meus dados",
            "meus dados pessoais",
            "proteção de dados",
            "lgpd",
        ),
    ),
```

In `app/evaluation/consumer_runner.py`, change `QUERY_BUILDER_VERSION` to `"consumer-legal-three-query-v4"`. Then update the two pinned versions:
- `tests/test_consumer_evaluation.py:274`: `"consumer-legal-three-query-v3"` → `"consumer-legal-three-query-v4"`;
- `tests/test_consumer_api.py:170`: `"consumer-notice-scope-eligibility-v2"` → `"consumer-notice-scope-eligibility-v3"`.

- [ ] **Step 6: Run the tests to verify they pass**

Run: `python -m pytest tests/test_legal_policy_sources.py tests/test_ground_selection.py tests/test_consumer_retrieval.py tests/test_consumer_grounds.py tests/test_consumer_api.py tests/test_consumer_evaluation.py -q`
Expected: all pass.

- [ ] **Step 7: Run the retrieval gates on the unchanged corpus**

Run the CI retrieval command (the full command is in Plan A, Task 4, Step 7).
Expected: exit code 0.

The `CDC` qualifier changes lexical scoring for CDC-only queries. If a gate fails, stop and report the per-case metrics from the JSON output; do not change thresholds or expansions without the maintainer's approval.

- [ ] **Step 8: Re-measure the notice baseline**

Run: `python -m pytest tests/test_consumer_notice_evaluation.py -q -k baseline`

- **If it passes**, the query change did not move final grounds on the CDC-only corpus.
- **If it fails**, the new queries changed which grounds are cited. Print the new totals:

  ```bash
  python -m app.evaluation.consumer_runner --evaluate-notice --output notice-results.json
  ```

  Then decide:
  - **Known-bad citations still at most 4:** update `test_offline_notice_baseline_on_the_seed_dataset` with the new grounds count and exact recall. Keep the known-bad assertion at the new value, and write "baseline moved from 79/4/0.333 to X/Y/Z" in the commit message.
  - **Known-bad citations above 4:** stop and report.

  Delete `notice-results.json` afterwards.

- [ ] **Step 9: Lint, type-check, commit**

Run: `python -m ruff check app tests frontend`, `python -m mypy app` and `python -m pytest --cov --cov-report=term-missing -q`. Expected: clean, with 100% on the covered modules.

```bash
git add app/consumer/legal_policy.py app/consumer/ground_selection.py app/consumer/retrieval.py app/evaluation/consumer_runner.py tests/test_legal_policy_sources.py tests/test_ground_selection.py tests/test_consumer_retrieval.py tests/test_consumer_api.py tests/test_consumer_evaluation.py tests/test_consumer_notice_evaluation.py
git commit -F - <<'EOF'
feat(consumer): make the LGPD and the Civil Code complement the CDC

LGPD chapters IV, VIII, IX and X are not citable; audit-only divisions never
are. At most three complementary grounds, only beside a CDC ground, and none
under lexical-only retrieval. Data-protection complaints get their own query
vocabulary, and numeric anchors now name the CDC.

Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>
Claude-Session: https://claude.ai/code/session_017ENVjKHCXDicafzUtJYJzA
EOF
```

---

### Task 8: Include the LGPD and the Civil Code in the default corpus

**Files:**
- Modify: `app/consumer/legal_corpus.py` (module docstring, `CONSUMER_LAW_CORPUS_RELEASE_ID`, `_default_provisions`, `as_parsed_document` warnings)
- Modify: `eval_data/consumer_legal_retrieval/dataset.json` (v1.2.0: new target corpus and 6 cases)
- Modify: `tests/test_consumer_legal_corpus.py`, `tests/test_legal_corpus_statutes.py`, `tests/test_ground_selection.py`, `tests/test_embedding_generation.py`, `tests/test_consumer_evaluation.py`, `tests/test_consumer_notice_evaluation.py`
- Modify: `.github/workflows/ci.yml` only if Step 9 obtains the maintainer's approval

**Interfaces:**
- Consumes: everything from Tasks 1–7.
- Produces: the default corpus, with 2,300 provisions (2,222 active), under release `br-consumer-law-<retrieved_on of the LGPD/CC manifests>-v4`.

- [ ] **Step 1: Write the failing tests**

In `tests/test_consumer_legal_corpus.py`, replace the assertions of `test_default_corpus_combines_full_cdc_with_reviewed_constitution`, from `assert corpus.release_id == ...` through the `verified_on` assertion, with:

```python
    lgpd = [item for item in corpus.provisions if item.source is LegalSource.DATA_PROTECTION_LAW]
    civil = [item for item in corpus.provisions if item.source is LegalSource.CIVIL_CODE]

    assert corpus.release_id == CONSUMER_LAW_CORPUS_RELEASE_ID
    assert corpus.release_id.endswith("-v4")
    assert len(corpus.provisions) == 2300
    assert (len(constitution), len(cdc), len(lgpd), len(civil)) == (7, 130, 80, 2083)
    assert len({item.provision_id for item in corpus.provisions}) == 2300
    assert {item.verified_on.isoformat() for item in (*cdc, *constitution)} == {"2026-08-04"}
    assert len({item.verified_on for item in (*lgpd, *civil)}) == 1
```

In `test_vetoed_articles_and_units_are_auditable_but_not_active`:
- change the set comprehension to only look at CDC provisions, adding `and item.source is LegalSource.CONSUMER_DEFENSE_CODE` to its condition;
- change `assert len(corpus.active_provisions) == 125` to `assert len(corpus.active_provisions) == 2222`.

In `test_amendment_only_articles_are_audited_but_never_retrievable`, append:

```python
    assert "br-lgpd-art-60" in audited
    assert "br-lgpd-art-60" not in chunked | retrievable
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `python -m pytest tests/test_consumer_legal_corpus.py -q`
Expected: FAIL with `assert 137 == 2300`, among other failures.

- [ ] **Step 3: Include the statutes and bump the release**

In `app/consumer/legal_corpus.py`:
1. Add `CIVIL_CODE` and `LGPD` to the `from app.consumer.statutes import (...)` block.
2. Set `CONSUMER_LAW_CORPUS_RELEASE_ID = "br-consumer-law-<D>-v4"`. `<D>` is the `retrieved_on` value of `app/consumer/data/lgpd/manifest.json` (both new manifests carry the same date). For example, `br-consumer-law-2026-09-14-v4` if Task 5 ran on 2026-09-14.
3. Replace `_default_provisions` with:

```python
def _default_provisions() -> tuple[LegalProvision, ...]:
    return (
        *_CONSTITUTION_PROVISIONS,
        *_statute_provisions(load_statute(CDC), _REVIEWED_CDC_METADATA),
        *_statute_provisions(load_statute(LGPD)),
        *_statute_provisions(load_statute(CIVIL_CODE)),
    )
```

4. Replace the `warnings=[...]` list in `as_parsed_document` with:

```python
            warnings=[
                "CDC, LGPD e Código Civil: texto oficial compilado de snapshots locais "
                "verificados por SHA-256.",
                "Código Civil: só a Parte Geral e o Livro I da Parte Especial entram no "
                "índice; os demais livros ficam no corpus para auditoria.",
                "CF: seleção de dispositivos transcritos da compilação oficial do Planalto.",
                "Unidades vetadas ou revogadas são mantidas para auditoria e marcadas.",
            ],
```

5. In the module docstring, replace its first paragraph with:

```text
The CDC, the LGPD and the Civil Code are built from integrity-checked offline
snapshots of the complete compiled statutes published by Planalto; only the
Civil Code's general part and law of obligations are indexed (ADR 0016). The
Constitution portion is a small reviewed set of consumer-relevant provisions
transcribed from the official compiled Constitution. Editorial summaries remain
visibly separate from the official text in every page and citation.
```

- [ ] **Step 4: Stop building test corpora that duplicate the default one**

The default corpus now contains the LGPD and the Civil Code, so adding them again would raise `duplicate provision ids`.
- In `tests/test_legal_corpus_statutes.py`, replace the body of `_with_civil_code()` with `return get_default_legal_corpus()`.
- In `tests/test_ground_selection.py`, replace the body of `_mixed_corpus()` with `return get_default_legal_corpus()`.
- Remove the imports these files no longer use (`_statute_provisions`, `load_statute`, `CIVIL_CODE`, `LGPD`, `LegalCorpus` where applicable).

- [ ] **Step 5: Stop pinning the shard count of the old corpus size**

In `tests/test_embedding_generation.py`:
1. Add `import math`.
2. In `test_generation_is_checksummed_active_and_strongly_validated`, replace `assert manifest.expected_shard_count == 3` with:

```python
    assert manifest.expected_shard_count == math.ceil(len(corpus.as_chunks()) / 200)
```

3. In `test_failed_generation_resumes_only_missing_shards`, replace `assert healthy_embedder.calls == 2` with:

```python
    assert healthy_embedder.calls == math.ceil(len(corpus.as_chunks()) / 200) - 1
```

- [ ] **Step 6: Extend the golden dataset to v1.2.0**

In `eval_data/consumer_legal_retrieval/dataset.json`, change `"version"` to `"1.2.0"` and `"description"` to:

```json
"Developer-authored seed for offline retrieval evaluation from lay Brazilian consumer complaints to CDC provisions, with LGPD and Civil Code complements. It is not a production legal golden until specialist review."
```

Append these six cases to `"cases"`, after `salario_atrasado`. Each carries a `law:*` slice so the reports split by source.

```json
    {
      "case_id": "vazamento_de_dados_cadastrais",
      "category": "personal_data",
      "intake_category": "other",
      "slices": ["supplier:retail", "remedy:information", "wording:lay", "law:lgpd"],
      "complaint": "Recebi um e-mail da loja avisando que houve um vazamento de dados dos clientes. Depois disso, meu nome, CPF e endereço foram usados para abrir um crediário em outra loja.",
      "desired_resolution": "Quero saber quais dados vazaram e que a loja responda pelos prejuízos.",
      "relevant": [
        {"article_id": "br-lgpd-art-46", "unit_id": "br-lgpd-art-46-caput", "grade": 3, "rationale": "Os agentes de tratamento devem adotar medidas de segurança aptas a proteger os dados pessoais de acessos não autorizados."},
        {"article_id": "br-lgpd-art-42", "unit_id": "br-lgpd-art-42-caput", "grade": 2, "rationale": "O controlador que causar dano em razão do tratamento de dados pessoais é obrigado a repará-lo."},
        {"article_id": "br-lgpd-art-48", "unit_id": "br-lgpd-art-48-caput", "grade": 2, "rationale": "O controlador deve comunicar ao titular incidente de segurança que possa acarretar risco ou dano relevante."},
        {"article_id": "br-cdc-art-14", "unit_id": "br-cdc-art-14-caput", "grade": 2, "rationale": "O fornecedor responde, independentemente de culpa, por defeitos na prestação do serviço, inclusive de segurança."}
      ],
      "hard_negatives": ["br-lgpd-art-52", "br-cdc-art-49"]
    },
    {
      "case_id": "pedido_de_exclusao_de_dados_ignorado",
      "category": "personal_data",
      "intake_category": "other",
      "slices": ["supplier:app", "remedy:deletion", "wording:lay", "law:lgpd"],
      "complaint": "Cancelei minha conta no aplicativo e pedi por e-mail para excluir meus dados pessoais. Três meses depois continuo recebendo mensagens de marketing e a empresa não responde.",
      "desired_resolution": "Quero que apaguem meus dados e confirmem a exclusão por escrito.",
      "relevant": [
        {"article_id": "br-lgpd-art-18", "unit_id": "br-lgpd-art-18-inciso-vi", "grade": 3, "rationale": "O titular tem direito à eliminação dos dados pessoais tratados com o seu consentimento."},
        {"article_id": "br-lgpd-art-18", "unit_id": "br-lgpd-art-18-inciso-ix", "grade": 2, "rationale": "O titular pode revogar o consentimento dado ao tratamento dos seus dados."}
      ],
      "hard_negatives": ["br-lgpd-art-55-j", "br-cdc-art-42"]
    },
    {
      "case_id": "compartilhamento_de_dados_sem_consentimento",
      "category": "personal_data",
      "intake_category": "other",
      "slices": ["supplier:telecom", "remedy:information", "wording:lay", "law:lgpd"],
      "complaint": "Descobri que a operadora de telefonia repassou meu número e meu endereço para empresas parceiras sem eu ter autorizado. Agora recebo ligações de telemarketing todos os dias.",
      "desired_resolution": "Quero que parem de compartilhar meus dados e informem com quem foram compartilhados.",
      "relevant": [
        {"article_id": "br-lgpd-art-18", "unit_id": "br-lgpd-art-18-inciso-vii", "grade": 3, "rationale": "O titular tem direito de saber com quais entidades o controlador compartilhou seus dados."},
        {"article_id": "br-lgpd-art-7", "unit_id": "br-lgpd-art-7-inciso-i", "grade": 2, "rationale": "O consentimento do titular é uma das hipóteses que autorizam o tratamento de dados pessoais."},
        {"article_id": "br-cdc-art-43", "unit_id": "br-cdc-art-43-caput", "grade": 1, "rationale": "O consumidor tem acesso às informações existentes em cadastros e dados pessoais arquivados sobre ele."}
      ],
      "hard_negatives": ["br-lgpd-art-52", "br-cdc-art-49"]
    },
    {
      "case_id": "pagamento_em_duplicidade_nao_devolvido",
      "category": "undue_payment",
      "intake_category": "unauthorized_charge",
      "slices": ["supplier:bank", "remedy:refund", "wording:lay", "law:cc"],
      "complaint": "Paguei a mesma fatura do cartão duas vezes por engano, pelo aplicativo do banco. O banco confirma que recebeu os dois pagamentos, mas se recusa a devolver o valor pago a mais.",
      "desired_resolution": "Quero a devolução do valor pago em duplicidade, com correção.",
      "relevant": [
        {"article_id": "br-cc-art-876", "unit_id": "br-cc-art-876-caput", "grade": 3, "rationale": "Quem recebeu o que não lhe era devido fica obrigado a restituir."},
        {"article_id": "br-cc-art-884", "unit_id": "br-cc-art-884-caput", "grade": 2, "rationale": "Quem se enriquece sem justa causa à custa de outrem deve restituir o indevidamente auferido."}
      ],
      "hard_negatives": ["br-cc-art-42", "br-lgpd-art-42"]
    },
    {
      "case_id": "atraso_na_devolucao_combinada",
      "category": "late_refund",
      "intake_category": "service_failure",
      "slices": ["supplier:retail", "remedy:refund", "wording:lay", "law:cc"],
      "complaint": "Cancelei a compra de um sofá e a loja se comprometeu por escrito a devolver o valor em dez dias. Já se passaram dois meses e o dinheiro não voltou.",
      "desired_resolution": "Quero o valor de volta com juros e correção pelo atraso.",
      "relevant": [
        {"article_id": "br-cc-art-395", "unit_id": "br-cc-art-395-caput", "grade": 3, "rationale": "O devedor responde pelos prejuízos a que sua mora der causa, com juros e atualização monetária."},
        {"article_id": "br-cc-art-406", "unit_id": "br-cc-art-406-caput", "grade": 1, "rationale": "Define a taxa legal de juros quando não houver taxa convencionada."}
      ],
      "hard_negatives": ["br-cdc-art-26", "br-cc-art-42"]
    },
    {
      "case_id": "renuncia_antecipada_em_contrato_de_adesao",
      "category": "adhesion_contract_clause",
      "intake_category": "other",
      "slices": ["supplier:service", "remedy:repair", "wording:lay", "law:cc"],
      "complaint": "Assinei um contrato de adesão para instalar energia solar. Uma cláusula diz que renuncio a qualquer indenização se a instalação der problema, e agora o sistema não funciona.",
      "desired_resolution": "Quero que a cláusula seja considerada sem efeito e que consertem o sistema.",
      "relevant": [
        {"article_id": "br-cdc-art-51", "unit_id": "br-cdc-art-51-inciso-i", "grade": 3, "rationale": "São nulas as cláusulas que impossibilitem, exonerem ou atenuem a responsabilidade do fornecedor por vícios."},
        {"article_id": "br-cc-art-424", "unit_id": "br-cc-art-424-caput", "grade": 2, "rationale": "Nos contratos de adesão, são nulas as cláusulas de renúncia antecipada do aderente a direito resultante do negócio."}
      ],
      "hard_negatives": ["br-cdc-art-49", "br-lgpd-art-46"]
    }
```

Then pin the new corpus:

Run: `python -c "from app.consumer.legal_corpus import get_default_legal_corpus as g; c = g(); print(c.release_id, c.corpus_sha256, len(c.as_chunks()))"`

Set `"target_corpus_release_id"` and `"target_corpus_sha256"` to the printed values. The chunk count should be close to the spec's estimate of about 2,457. Write the exact number in the pull request description.

In `tests/test_consumer_evaluation.py`, `test_seed_dataset_is_separate_versioned_and_explicitly_unreviewed`:
- change `"1.1.0"` to `"1.2.0"` and `== 15` to `== 21`;
- append:

```python
    assert sum("law:lgpd" in case.slices for case in dataset.cases) == 3
    assert sum("law:cc" in case.slices for case in dataset.cases) == 3
```

- [ ] **Step 7: Guard the original cases in the notice evaluation**

Append to `tests/test_consumer_notice_evaluation.py`:

```python
_ORIGINAL_CASES = frozenset(
    {
        "produto_duravel_com_vicio",
        "servico_de_reparo_malfeito",
        "oferta_nao_entregue",
        "arrependimento_compra_online",
        "publicidade_enganosa_por_omissao",
        "venda_casada_seguro",
        "cobranca_indevida_ja_paga",
        "cobranca_com_ameacas",
        "negativacao_sem_aviso",
        "contrato_ilegivel_e_limitacao_oculta",
        "interrupcao_servico_essencial",
        "alergeno_omitido_no_rotulo",
        "superendividamento_e_minimo_existencial",
        "conflito_entre_vizinhos",
        "salario_atrasado",
    }
)


async def test_the_expansion_adds_no_known_bad_citation_to_the_original_cases() -> None:
    """Spec acceptance criterion 6: at most 4 on the 15 pre-expansion cases."""

    summary = await run_notice_evaluation(load_consumer_legal_dataset(DATASET_PATH))
    original = [case for case in summary.cases if case.case_name in _ORIGINAL_CASES]

    assert len(original) == 15
    assert sum(case.counts["consumer_notice_known_bad_citations"] for case in original) <= 4
```

- [ ] **Step 8: Run the suite and re-measure**

Run: `python -m pytest -q`
Expected: every test passes except, possibly, `test_offline_notice_baseline_on_the_seed_dataset`, whose pinned totals describe the old dataset.

Run: `python -m app.evaluation.consumer_runner --evaluate-notice --output notice-results.json`

From `totals` and `averages` in `notice-results.json`, update `test_offline_notice_baseline_on_the_seed_dataset` with the measured grounds, known-bad citations, complementary grounds and exact recall. Keep `summary.failed_case_count == 0` and the abstention and semantic-success assertions.

Write in the pull request description:
- the old and new values;
- the complementary grounds per case, from the per-case `counts`.

Delete `notice-results.json`.

- [ ] **Step 9: Run both CI gates exactly as CI does**

- **Retrieval gates:** run the command from Plan A, Task 4, Step 7.
  Expected: exit code 0. If a gate fails, stop and report the per-case metrics of the new and old cases. Do not change thresholds or remove cases.
- **Notice gate:**

  ```bash
  python -m app.evaluation.consumer_runner eval_data/consumer_legal_retrieval --evaluate-notice --output consumer-notice-results.json --max consumer_notice_known_bad_citations=4 --min consumer_notice_abstention=1.0
  ```

  If it exits 0, done. If the total exceeds 4 only because of the six new cases (the Step 7 test passes), stop and ask the maintainer whether to raise the CI ceiling to the measured total. Change `.github/workflows/ci.yml` only after approval, and say so in the commit message.

Delete the generated JSON files.

- [ ] **Step 10: Lint, type-check, commit**

Run: `python -m ruff check app tests frontend`, `python -m mypy app`, `python -m pytest --cov --cov-report=term-missing -q`, `lint-imports` and `vulture app --min-confidence 90`. Expected: all clean.

```bash
git add app/consumer/legal_corpus.py eval_data/consumer_legal_retrieval/dataset.json tests
git commit -F - <<'EOF'
feat(consumer): include the LGPD and the Civil Code in corpus release v4

2,300 provisions (CF 7, CDC 130, LGPD 80, Civil Code 2,083). Golden seed
1.2.0 adds three LGPD and three Civil Code cases; known-bad citations on the
original 15 cases stay at 4 or fewer.

Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>
Claude-Session: https://claude.ai/code/session_017ENVjKHCXDicafzUtJYJzA
EOF
```

---

### Task 9: Decision record, documentation and user-facing texts

**Files:**
- Create: `docs/adr/0016-multi-statute-consumer-corpus.md`
- Modify: `docs/adr/0012-bounded-consumer-extrajudicial-notice.md` and `docs/adr/0013-versioned-consumer-law-retrieval.md` (status lines)
- Modify: `docs/architecture.md`, `README.md`, `README-pt.md`
- Modify: `frontend/consumer_view.py` (consumer disclaimer), `app/consumer/settlement.py` (caveat)

In the READMEs, `N` below stands for the exact indexed chunk count written in the pull request description in Task 8, Step 6.

- [ ] **Step 1: Write ADR 0016**

Create `docs/adr/0016-multi-statute-consumer-corpus.md`:

```markdown
# ADR 0016: LGPD and a scoped Civil Code as complementary sources

## Status

Accepted · Date: 2026-09-12 · Amends: [ADR 0012](0012-bounded-consumer-extrajudicial-notice.md) §3
and [ADR 0013](0013-versioned-consumer-law-retrieval.md) §1

## Context

The Consumer corpus held the complete CDC and seven constitutional provisions.
Consumer disputes about personal data (breaches, sharing without consent,
ignored deletion requests) are governed by the LGPD, and many consumer claims
also rest on general civil-law rules: undue payment, unjust enrichment,
late-payment interest, adhesion contracts, civil liability. Planalto publishes
both statutes as compiled HTML with their own quirks. The Civil Code has 2,083
articles; most concern companies, property, family and succession, which a
notice to a supplier never rests on. The ground selector already cites some
unrelated provisions because it relies on retrieval agreement (RAG review of
2026-09-07), and a five-times larger index widens that exposure.

## Decision

1. Parse every statute with one generic Planalto parser driven by a
   declarative specification per law: article range, declared gaps, exact text
   corrections and index scope. Parsing fails closed, and the CDC output is
   byte-identical to the former CDC parser.
2. Pin each snapshot with a manifest recording the raw SHA-256, the SHA-256 of
   the extracted text (Planalto's WAF randomizes raw bytes), the download user
   agent and a review status.
3. Index the whole LGPD. Index only the Civil Code's general part and law of
   obligations; keep the other books in the corpus as `audit_only`.
4. A notice may cite LGPD chapters I–III and V–VII and indexed Civil Code
   provisions. LGPD chapters IV, VIII, IX and X are never cited.
5. LGPD and Civil Code grounds are complementary: at most three per notice,
   only beside at least one CDC ground, and none when retrieval fell back to
   lexical-only search. The CDC remains the primary authority.
6. Data-protection complaints get a retrieval vocabulary without article
   numbers, and numeric anchors in the existing expansions name the CDC,
   because the same numbers now exist in other statutes.
7. The consumer-scope gate is unchanged: the new sources are consulted only
   for consumer disputes.

## Consequences

- (+) Notices can cite data-protection duties and general civil-law rules
  with the same provenance and citation checks as the CDC.
- (+) A changed official page fails the parse or the text hash instead of
  silently changing the law.
- (-) The index grows from 472 to about 2,460 chunks. The first build reuses
  the 472 existing vectors (ADR 0017) and still embeds about 1,985 chunks.
- (-) Factual applicability is still not verified; the cap and the CDC anchor
  bound unsupported complementary grounds but do not remove them.
- (-) The Civil Code scope, the LGPD eligibility rules and the new golden
  labels require specialist legal review.
```

- [ ] **Step 2: Point the amended ADRs at 0016**

- In `docs/adr/0012-bounded-consumer-extrajudicial-notice.md`, replace `Status: accepted · Date: 2026-08-04 · Amended: 2026-08-24` with `Status: accepted · Date: 2026-08-04 · Amended: 2026-08-24 · Amended by [ADR 0016](0016-multi-statute-consumer-corpus.md)`.
- In `docs/adr/0013-versioned-consumer-law-retrieval.md`, replace the status body `Accepted` with `Accepted · Amended by [ADR 0016](0016-multi-statute-consumer-corpus.md)`.

- [ ] **Step 3: Update `docs/architecture.md`**

1. In the mermaid diagram, replace `    RAG --> LAW[Versioned CDC and selected CF corpus]` with `    RAG --> LAW[Versioned CDC, LGPD, scoped Civil Code and selected CF]`.
2. Replace the paragraph under `## Legal provenance` that starts "The CDC snapshot is stored with retrieval date" with:

```markdown
The CDC, LGPD and Civil Code snapshots are stored with retrieval date, official
URL, raw and extracted-text SHA-256 and a review status, and are read by one
generic statute parser. Corpus parsing preserves stable provision/subdivision
IDs, hierarchy and source hashes. Only the Civil Code's general part and law of
obligations are indexed; its other books are kept for audit. Selected CF
provisions use the same typed legal schema. Runtime requests never fetch or
silently update law.
```

3. In the ADR index, insert after the `0015` line:

```markdown
- [0016](adr/0016-multi-statute-consumer-corpus.md) — LGPD and scoped Civil Code as complementary sources
```

- [ ] **Step 4: Update `README.md`**

1. Replace `        |      +--> versioned CDC + selected CF provisions` with `        |      +--> versioned CDC, LGPD, scoped Civil Code + selected CF`.
2. Replace the two bullets that begin "- The CDC is ingested from a pinned Planalto snapshot" and "- Selected constitutional provisions are versioned" with:

```markdown
- The CDC, the LGPD and the Civil Code are ingested from pinned Planalto
  snapshots by one generic statute parser, with manifests pinning raw and
  extracted-text hashes. Only the Civil Code's general part and law of
  obligations are indexed; its other books stay in the corpus for audit.
- Selected constitutional provisions are versioned in the legal corpus.
- LGPD and Civil Code grounds complement the CDC: at most three per notice,
  only beside a CDC ground, and none under lexical-only retrieval.
```

3. Replace `  consumer-protection system.` (end of the eligibility bullet) with:

```markdown
  consumer-protection system. The LGPD chapters on processing by public
  bodies, administrative sanctions, the national authority and final
  provisions are excluded in the same way.
```

4. Replace `472 persisted legal chunks` with `N persisted legal chunks`.
5. After the paragraph that ends "does not retroactively prove metadata that the legacy run failed to record.", add:

````markdown
Statute snapshots are refreshed explicitly, never at runtime:

```powershell
python -m app.consumer.update_statute_snapshot --law lgpd
```

The refresher records the user agent it used, writes nothing when the
extracted text is unchanged, and writes new snapshots as `pending_review`;
compare a sample of articles with the official page before promoting a
snapshot and building a new corpus release.
````

6. After the limitation bullet "- The constitutional corpus contains selected provisions, not the complete Constitution.", add:

```markdown
- Only the Civil Code's general part and law of obligations are indexed; that
  scope, the LGPD eligibility rules and the new golden cases still need
  specialist review.
```

- [ ] **Step 5: Update `README-pt.md`**

1. Replace `        |      +--> CDC versionado + dispositivos selecionados da CF` with `        |      +--> CDC, LGPD e Código Civil (escopo limitado) + dispositivos da CF`.
2. Replace the two bullets that begin "- O CDC vem de snapshot fixado do Planalto" and "- Dispositivos constitucionais selecionados" with:

```markdown
- CDC, LGPD e Código Civil vêm de snapshots fixados do Planalto, lidos por um
  único parser de leis, com manifestos que fixam os hashes do arquivo e do texto
  extraído. Do Código Civil só a Parte Geral e o Livro I da Parte Especial entram
  no índice; os demais livros ficam no corpus para auditoria.
- Dispositivos constitucionais selecionados são versionados no corpus.
- Fundamentos da LGPD e do Código Civil complementam o CDC: no máximo três por
  notificação, só ao lado de um fundamento do CDC e nenhum em recuperação apenas
  lexical.
```

3. Replace `a API reutiliza os 460 chunks persistidos` with `a API reutiliza os N chunks persistidos`.
4. After the paragraph that ends "de metadados que a execução antiga não registrou.", add:

````markdown
Os snapshots das leis são atualizados explicitamente, nunca em tempo de execução:

```powershell
python -m app.consumer.update_statute_snapshot --law lgpd
```

O refresher registra o user agent usado, não grava nada quando o texto extraído
não mudou e grava snapshots novos como `pending_review`; confira artigos por
amostragem na página oficial antes de promover o snapshot e de gerar um novo
release do corpus.
````

5. After the limitation bullet about the constitutional corpus, add:

```markdown
- Do Código Civil só a Parte Geral e o Livro I da Parte Especial são indexados;
  esse escopo, as regras de elegibilidade da LGPD e os novos casos do golden
  ainda exigem revisão jurídica especializada.
```

- [ ] **Step 6: Update the user-facing texts**

In `frontend/consumer_view.py`, replace

```python
        "A ferramenta prepara um rascunho com referências à Constituição e ao "
        "Código de Defesa do Consumidor. Revise o documento com um advogado antes "
```

with

```python
        "A ferramenta prepara um rascunho com referências ao Código de Defesa do "
        "Consumidor e, quando pertinentes, à Constituição, à LGPD e ao Código Civil. "
        "Revise o documento com um advogado antes "
```

In `app/consumer/settlement.py`, replace

```python
                "A Constituição e o CDC não fornecem, por si sós, probabilidades ou tabelas "
                "de indenização para o caso concreto.",
```

with

```python
                "As normas citadas (CDC, Constituição, LGPD e Código Civil) não fornecem, "
                "por si sós, probabilidades ou tabelas de indenização para o caso concreto.",
```

- [ ] **Step 7: Verify and commit**

Run: `python -m pytest -q`, `python -m ruff check app tests frontend` and `python -m mypy app`. Expected: all clean.

```bash
git add docs/adr docs/architecture.md README.md README-pt.md frontend/consumer_view.py app/consumer/settlement.py
git commit -F - <<'EOF'
docs: record ADR 0016 and document the LGPD and Civil Code corpus

Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>
Claude-Session: https://claude.ai/code/session_017ENVjKHCXDicafzUtJYJzA
EOF
```

---

### Task 10: Reindex with vector reuse and roll out

This task is operational. It runs after the pull request with Tasks 1–9 is merged. It needs the maintainer's machine and hours of CPU time.

**Files:** none are committed. The results go into the pull request or its follow-up comment.

- [ ] **Step 1: Keep the running API on the old index**

Do not restart the API yet. With the new code it would refuse to generate notices until the new index exists; that is the current fail-fast behaviour.

- [ ] **Step 2: Build the new generation**

Run on the host that built generation `7a7b20a89b67c75d`:

```bash
python -m app.consumer.preindex_legal
```

Alternatively, run `docker compose --profile tools run --rm indexer`. Expected output ends with:
- `Índice legal indexado com sucesso …`
- `Vetores reaproveitados de gerações anteriores: 472 (fontes: 7a7b20a89b67c75d)`

About 1,985 chunks are embedded, in resumable shards of 25. At the speed of the last run this can take several days of wall-clock time on this CPU. If a GPU is available, `LITIGATION_EMBEDDING_DEVICE=cuda` shortens it a lot. Interrupting and re-running resumes from the last verified shard.

If the build stops with `ReuseCanaryError`, the reused vectors do not match the current model. Typical causes are a different platform or a changed formatter. Re-run on the host that produced the source generation. If that is not possible, run `python -m app.consumer.preindex_legal --no-reuse`, which embeds all ≈2,460 chunks.

- [ ] **Step 3: Check readiness and restart**

Run: `python -m app.consumer.preindex_legal --check`
Expected: exit code 0, `Índice legal pronto`, and the corpus release `br-consumer-law-<D>-v4`.
Restart the API. `GET /health` must report the legal corpus as ready.

- [ ] **Step 4: Measure the configured stack**

```bash
python -m app.evaluation.consumer_runner --retriever app.evaluation.consumer_retrievers:configured_hybrid_retriever --output configured-v4.json
python -m app.evaluation.consumer_runner --evaluate-notice --notice-pipeline configured --require-semantic --output configured-notice-v4.json
```

Compare `configured-v4.json` with the tracked `configured.json` (release v3) metric by metric: recall, article recall, nDCG, hard-negative rate. Report the notice totals from `configured-notice-v4.json`, per case and split by the `law:*` slices. Put both comparisons in the pull request. Do not commit the JSON files, and do not modify `configured.json`, `results.json` or `docs/rag-review/`.

- [ ] **Step 5: Leave old data in place**

Keep the old PostgreSQL namespace and the generations `7a7b20a89b67c75d`, `5f2ab81183320675` and `7dbbe8b236f85986` until the new index has served real traffic. Deleting them is a separate, manual decision. The new generation becomes the reuse source for the next corpus release.

---

## Notes for the executor

- This plan is the only one that touches the network (Task 5) and the only one that needs long CPU time (Task 10).
- Wherever this plan pins a value (text hashes, counts, digests), a mismatch is a finding. Stop and report it instead of editing the expected value. There are two exceptions, both explicitly marked as re-measurements: the notice baseline (Task 7 Step 8 and Task 8 Step 8) and the golden target release and hash.
- Every new golden label and policy remains `requires_legal_review`; nothing here amounts to legal validation.
