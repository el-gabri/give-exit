# Lay-Language Alias Chunks — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Give each citable CDC, LGPD and Constitution unit a few plain-language access points, indexed as a separate alias chunk that is never quoted, so lay complaints can reach the right article.

**Architecture:** A versioned alias file (`app/consumer/data/aliases/aliases.json`, schema in `app/consumer/aliases.py`) is produced offline by a generator (`app/consumer/alias_generation.py`) that sends only statute text to the configured LLM. `LegalCorpus` validates the file against the statute, adds one alias chunk per aliased unit, folds the aliases into the corpus identity, and resolves an alias hit to the unit's official text when building a citation. A corpus release (v5) and the golden dataset (2.3.0) follow the real generation run, measured with the ADR 0021 evaluation.

**Tech Stack:** Python 3.10+/3.12, pydantic v2, pytest + pytest-asyncio (auto mode), ruff, mypy strict, OpenAI through `app.llm` (faked in tests), JUÁ 4B via sentence-transformers (configured-stack protocol only).

**Spec:** `docs/superpowers/specs/2026-10-03-lay-language-alias-chunks-design.md`

## Global Constraints

- A citation always quotes official text; an alias is never quoted (spec D4).
- No LLM call at query time; the runtime stays deterministic.
- The generator never reads `eval_data/`; no golden complaint or remedy may appear in a request.
- Coverage: active units of citable `br-cdc`, `br-lgpd` and `br-cf` provisions (584 units, 108 articles). The Civil Code gets no aliases.
- Alias rules: 2–4 aliases per entry, each 12–220 characters after whitespace normalisation, distinct within the entry, and matching none of `§` or the words *art/arts/artigo(s), lei(s), inciso(s), parágrafo(s), alínea(s), CDC, LGPD, código(s)* (case- and accent-insensitive). Digits are allowed.
- `generated` and `reviewed` entries are indexed; `rejected` entries are not. An entry whose `source_sha256` no longer matches its unit fails corpus load.
- An empty alias set leaves the corpus identity of earlier releases unchanged; a non-empty set makes the corpus hash payload `schema_version` 3.
- Release after generation: `CONSUMER_LAW_CORPUS_RELEASE_ID = "br-consumer-law-<date of the real generation run>-v5"`, `LEGAL_CHUNKING_VERSION = "legal-hierarchy-v5"`, golden dataset 2.3.0 (labels and splits unchanged).
- Holdout results are reported, never used to adjust anything (ADR 0021).
- Stop rules after the real run: stop and report if development `consumer_notice_known_bad_citations` > 0, or development `consumer_notice_precision` or `consumer_notice_article_recall` falls below the v4 baseline (0.042 and 0.025 offline).
- Ruff line length 100, complexity ≤ 10, mypy strict, Python floor 3.10. `app.consumer.aliases` and `app.consumer.alias_generation` join the 100% coverage scope; mark only `if __name__ == "__main__":` lines `# pragma: no cover`.
- Tests building `Settings` pass `_env_file=None` or an explicit model; the LLM is always faked in tests.
- On this machine run Python as `.venv/Scripts/python.exe`. JUÁ commands need `HF_HUB_OFFLINE=1 TRANSFORMERS_OFFLINE=1 OMP_NUM_THREADS=2`.
- `tests/test_demo_manifest.py` fails on `main` already (commit ed1c004 deleted `demo/`); deselect it in full-suite runs and say so.
- Every commit message ends with `Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>`.
- `$WS` below is this plan's executor workspace, `.superpowers/sdd/2026-10-03-lay-language-alias-chunks/`.

## Review Focus

1. A reviewer hand-edits `aliases.json` in an editor that adds a byte-order mark and Windows line endings — it must still load — Task 1.
2. A hand edit that breaks a rule (a legal term, a duplicate) must fail with a message naming the unit, so the reviewer can find it among 584 entries — Task 1.
3. The model answers with a unit it was not asked about, or the same unit twice — extras are ignored and the first answer wins — Task 4.
4. The provider fails halfway through the 108 articles — finished articles stay saved, the CLI exits 1 saying to rerun, and a rerun resumes without repeating calls — Task 4.
5. `--article` names an unknown or uncovered article (e.g. a Civil Code one) — exit 2 with a message, not a traceback — Task 4.

---

### Task 1: The alias schema and file I/O

**Files:**
- Create: `app/consumer/aliases.py`
- Modify: `pyproject.toml` (coverage source)
- Test: `tests/test_consumer_aliases.py`

**Interfaces:**
- Produces, in `app.consumer.aliases`: `ALIASES_PATH: Path`, `ALIAS_LAW_IDS: frozenset[str]`, `AliasStatus = Literal["generated", "reviewed", "rejected"]`, `INDEXED_ALIAS_STATUSES: frozenset[str]`, `normalize_alias(text: str) -> str`, `alias_problem(text: str) -> str | None`, `AliasEntry(unit_key, provision_id, source_sha256, aliases: tuple[str, ...], status="generated")`, `AliasSet(schema_version=1, review_status="requires_legal_review", prompt_version: str, prompt_sha256: str | None = None, model: str | None = None, generated_on: date | None = None, entries: tuple[AliasEntry, ...] = ())` with `.indexed` and `.identity() -> dict[str, object]`, `load_alias_set(path=ALIASES_PATH) -> AliasSet`, `write_alias_set(alias_set, path=ALIASES_PATH) -> None`.

- [ ] **Step 1: Write the failing tests**

Create `tests/test_consumer_aliases.py`:

```python
"""Lay-language alias entries: rules, the set's identity and file I/O."""

from __future__ import annotations

from datetime import date
from pathlib import Path
from typing import Any

import pytest
from pydantic import ValidationError

from app.consumer.aliases import (
    AliasEntry,
    AliasSet,
    alias_problem,
    load_alias_set,
    normalize_alias,
    write_alias_set,
)

SHA = "a" * 64
FIRST = "A loja só me vendeu o celular se eu levasse a capinha junto."
SECOND = "O banco disse que só liberava o empréstimo se eu fizesse um seguro."


def _entry(**overrides: Any) -> AliasEntry:
    payload: dict[str, Any] = {
        "unit_key": "br-cdc-art-39-inciso-i",
        "provision_id": "br-cdc-art-39",
        "source_sha256": SHA,
        "aliases": [FIRST, SECOND],
    }
    payload.update(overrides)
    return AliasEntry.model_validate(payload)


def test_aliases_are_normalised_and_default_to_generated() -> None:
    entry = _entry(aliases=[f"  {FIRST.replace(' ', '   ')} ", SECOND])

    assert entry.aliases == (FIRST, SECOND)
    assert entry.status == "generated"
    assert normalize_alias("  a\n b\t c ") == "a b c"


@pytest.mark.parametrize(
    "text",
    [
        "Isso fere o art. 39 do que vale aqui.",
        "Segundo a LEI, a loja não podia fazer isso.",
        "Está escrito no parágrafo único da regra.",
        "O CDC proíbe que a loja faça isso comigo.",
        "A lgpd diz que eles não podiam guardar isso.",
        "Conforme o § 2º, a cobrança não podia vir.",
        "O Código diz que eu tenho esse direito sim.",
        "Os incisos garantem que eu receba de volta.",
    ],
)
def test_an_alias_that_cites_the_law_is_refused_naming_the_unit(text: str) -> None:
    assert alias_problem(text) is not None
    with pytest.raises(ValidationError, match="br-cdc-art-39-inciso-i: alias"):
        _entry(aliases=[text, SECOND])


@pytest.mark.parametrize(
    "text",
    [
        "Comprei leite estragado no mercado ontem à noite.",
        "A arte da embalagem era diferente da foto do site.",
        "Desisti da compra em sete dias e não devolveram nada.",
    ],
)
def test_everyday_words_that_look_like_legal_terms_pass(text: str) -> None:
    assert alias_problem(text) is None


@pytest.mark.parametrize(
    ("aliases", "message"),
    [
        (["curto", SECOND], "shorter than 12"),
        (["x" * 221, SECOND], "longer than 220"),
        ([FIRST, FIRST.upper()], "distinct"),
        ([FIRST], "at least 2"),
        ([FIRST, SECOND, "Terceira frase leiga bem comum.", "Quarta frase leiga bem comum.",
          "Quinta frase leiga bem comum."], "at most 4"),
    ],
)
def test_alias_bounds(aliases: list[str], message: str) -> None:
    with pytest.raises(ValidationError, match=message):
        _entry(aliases=aliases)


def test_an_entry_must_belong_to_its_provision() -> None:
    with pytest.raises(ValidationError, match="br-cdc-art-40-caput: not a unit of br-cdc-art-39"):
        _entry(unit_key="br-cdc-art-40-caput")
    assert _entry(unit_key="br-cf-art-5-xxxii", provision_id="br-cf-art-5-xxxii").aliases


def test_the_set_rejects_duplicate_units_and_hashes_only_indexed_entries() -> None:
    with pytest.raises(ValidationError, match="duplicate alias entries: br-cdc-art-39-inciso-i"):
        AliasSet(prompt_version="p", entries=(_entry(), _entry()))
    rejected = _entry(unit_key="br-cdc-art-39-inciso-ii", status="rejected")
    alias_set = AliasSet(
        prompt_version="p", model="m", generated_on=date(2026, 10, 3), entries=(_entry(), rejected)
    )

    identity = alias_set.identity()

    assert [entry.unit_key for entry in alias_set.indexed] == ["br-cdc-art-39-inciso-i"]
    assert [item["unit_key"] for item in identity["entries"]] == ["br-cdc-art-39-inciso-i"]
    assert identity["generated_on"] == "2026-10-03"
    reviewed = AliasSet(
        prompt_version="p",
        model="m",
        generated_on=date(2026, 10, 3),
        entries=(_entry(status="reviewed"), rejected),
    )
    assert reviewed.identity() != identity


def test_the_file_round_trips(tmp_path: Path) -> None:
    path = tmp_path / "aliases.json"
    alias_set = AliasSet(prompt_version="p", prompt_sha256="b" * 64, entries=(_entry(),))

    write_alias_set(alias_set, path)

    assert load_alias_set(path) == alias_set
    assert path.read_text(encoding="utf-8").endswith("\n")
    assert "empréstimo" in path.read_text(encoding="utf-8")


def test_a_hand_edited_file_with_bom_and_crlf_loads(tmp_path: Path) -> None:
    path = tmp_path / "aliases.json"
    write_alias_set(AliasSet(prompt_version="p", entries=(_entry(),)), path)
    text = path.read_text(encoding="utf-8").replace("\n", "\r\n")
    path.write_bytes(b"\xef\xbb\xbf" + text.encode("utf-8"))

    assert load_alias_set(path).entries[0].aliases == (FIRST, SECOND)
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `.venv/Scripts/python.exe -m pytest tests/test_consumer_aliases.py -q -p no:cacheprovider`
Expected: FAIL — `ModuleNotFoundError: No module named 'app.consumer.aliases'`.

- [ ] **Step 3: Create `app/consumer/aliases.py`**

```python
"""Lay-language aliases: plain-language access points to citable legal units.

A lay complaint rarely shares words with the statute, so the retrieval
agreement gate could never support the unit it needs (ADR 0019). Each entry
gives one unit a few sentences in a consumer's own words; the corpus indexes
them as a separate alias chunk, which is never quoted (ADR 0022).

The file is generated offline by ``python -m app.consumer.generate_aliases``
from statute text alone and is reviewed like the rest of the corpus.
"""

from __future__ import annotations

import json
import re
import unicodedata
from datetime import date
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from app.consumer.embedding_artifacts import atomic_write

ALIASES_PATH = Path(__file__).resolve().parent / "data" / "aliases" / "aliases.json"
# Laws whose units may carry aliases (ADR 0022). The Civil Code is
# complementary, and its lay matches drifted off-topic before.
ALIAS_LAW_IDS = frozenset({"br-cdc", "br-lgpd", "br-cf"})
AliasStatus = Literal["generated", "reviewed", "rejected"]
INDEXED_ALIAS_STATUSES: frozenset[str] = frozenset({"generated", "reviewed"})
MIN_ALIAS_CHARS = 12
MAX_ALIAS_CHARS = 220
_LEGAL_ID = r"^br-(?:cdc|cf|lgpd|cc)-art-[a-z0-9]+(?:-[a-z0-9]+)*$"
# An alias is a lay sentence. One that names an article, a statute or a code
# could carry a citation into the index, so it is refused.
_LEGAL_REFERENCE = re.compile(
    r"§|\b(?:arts?|artigos?|leis?|incisos?|paragrafos?|alineas?|cdc|lgpd|codigos?)\b"
)


def normalize_alias(text: str) -> str:
    """The stored form of an alias: whitespace collapsed."""
    return " ".join(text.split())


def alias_problem(text: str) -> str | None:
    """Why ``text`` cannot be an alias, or None when it can."""
    if len(text) < MIN_ALIAS_CHARS:
        return f"is shorter than {MIN_ALIAS_CHARS} characters"
    if len(text) > MAX_ALIAS_CHARS:
        return f"is longer than {MAX_ALIAS_CHARS} characters"
    decomposed = unicodedata.normalize("NFKD", text.casefold())
    folded = "".join(char for char in decomposed if not unicodedata.combining(char))
    match = _LEGAL_REFERENCE.search(folded)
    if match:
        return f"cites the law ({match.group(0)!r})"
    return None


class AliasEntry(BaseModel):
    """The aliases of one unit (or of a provision without units)."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    unit_key: str = Field(pattern=_LEGAL_ID)
    provision_id: str = Field(pattern=_LEGAL_ID)
    source_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    aliases: tuple[str, ...] = Field(min_length=2, max_length=4)
    status: AliasStatus = "generated"

    @field_validator("aliases", mode="before")
    @classmethod
    def _normalized(cls, values: object) -> object:
        if isinstance(values, (list, tuple)):
            return tuple(normalize_alias(str(value)) for value in values)
        return values

    @model_validator(mode="after")
    def _valid(self) -> AliasEntry:
        # Messages name the unit, so a reviewer can find it among hundreds.
        own = self.unit_key == self.provision_id or self.unit_key.startswith(
            f"{self.provision_id}-"
        )
        if not own:
            raise ValueError(f"{self.unit_key}: not a unit of {self.provision_id}")
        for alias in self.aliases:
            problem = alias_problem(alias)
            if problem:
                raise ValueError(f"{self.unit_key}: alias {alias!r} {problem}")
        if len({alias.casefold() for alias in self.aliases}) != len(self.aliases):
            raise ValueError(f"{self.unit_key}: aliases must be distinct")
        return self


class AliasSet(BaseModel):
    """The alias file: a manifest and one entry per aliased unit."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    schema_version: Literal[1] = 1
    review_status: Literal["requires_legal_review", "legally_reviewed"] = "requires_legal_review"
    prompt_version: str = Field(min_length=1)
    prompt_sha256: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    model: str | None = None
    generated_on: date | None = None
    entries: tuple[AliasEntry, ...] = ()

    @model_validator(mode="after")
    def _unique_units(self) -> AliasSet:
        seen: set[str] = set()
        duplicates: set[str] = set()
        for entry in self.entries:
            if entry.unit_key in seen:
                duplicates.add(entry.unit_key)
            seen.add(entry.unit_key)
        if duplicates:
            raise ValueError("duplicate alias entries: " + ", ".join(sorted(duplicates)))
        return self

    @property
    def indexed(self) -> tuple[AliasEntry, ...]:
        """The entries the corpus indexes."""
        return tuple(entry for entry in self.entries if entry.status in INDEXED_ALIAS_STATUSES)

    def identity(self) -> dict[str, object]:
        """What the corpus hash covers: the manifest and every indexed entry."""
        return {
            "schema_version": self.schema_version,
            "review_status": self.review_status,
            "prompt_version": self.prompt_version,
            "prompt_sha256": self.prompt_sha256,
            "model": self.model,
            "generated_on": self.generated_on.isoformat() if self.generated_on else None,
            "entries": [
                {
                    "unit_key": entry.unit_key,
                    "source_sha256": entry.source_sha256,
                    "aliases": list(entry.aliases),
                    "status": entry.status,
                }
                for entry in self.indexed
            ],
        }


def load_alias_set(path: Path = ALIASES_PATH) -> AliasSet:
    """Read an alias file; a byte-order mark or Windows line endings are fine."""
    return AliasSet.model_validate(json.loads(path.read_text(encoding="utf-8-sig")))


def write_alias_set(alias_set: AliasSet, path: Path = ALIASES_PATH) -> None:
    rendered = json.dumps(alias_set.model_dump(mode="json"), ensure_ascii=False, indent=2)
    atomic_write(path, (rendered + "\n").encode("utf-8"))
```

In `pyproject.toml`, add `"app.consumer.aliases",` to `[tool.coverage.run] source` in alphabetical position.

- [ ] **Step 4: Run the tests to verify they pass**

Run: `.venv/Scripts/python.exe -m pytest tests/test_consumer_aliases.py -q -p no:cacheprovider --cov=app.consumer.aliases --cov-branch --cov-report=term-missing`
Expected: PASS, `app/consumer/aliases.py` 100%.

- [ ] **Step 5: Lint, type-check, commit**

```bash
.venv/Scripts/python.exe -m ruff check app tests && .venv/Scripts/python.exe -m mypy app
git add app/consumer/aliases.py pyproject.toml tests/test_consumer_aliases.py
git commit -m "feat(consumer): lay-language alias schema and file I/O

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

---

### Task 2: Alias chunks in the legal corpus

**Files:**
- Modify: `app/consumer/legal_corpus.py`
- Create: `app/consumer/data/aliases/aliases.json`
- Test: `tests/test_consumer_alias_chunks.py`

**Interfaces:**
- Consumes: `AliasEntry`, `AliasSet`, `ALIAS_LAW_IDS`, `INDEXED_ALIAS_STATUSES`, `load_alias_set` (Task 1).
- Produces: `LegalCorpus(provisions, *, aliases: AliasSet | None = None)`; `LegalCorpus.aliases -> AliasSet | None`; `LegalCorpus.aliasable_units(provision) -> tuple[LegalTextUnit | None, ...]` (static); `LegalCorpus.alias_source_block(provision, unit) -> str`; `default_legal_provisions() -> tuple[LegalProvision, ...]` (renamed from `_default_provisions`); alias chunk id `f"{document_id}:legal:{unit_key}:alias-01"` with metadata `chunk_level="alias"`, `content_kind="lay_alias"`.

- [ ] **Step 1: Write the failing tests**

Create `tests/test_consumer_alias_chunks.py`:

```python
"""Alias chunks: one per aliased unit, validated against the statute."""

from __future__ import annotations

from functools import lru_cache

import pytest

from app.consumer.aliases import AliasEntry, AliasSet
from app.consumer.legal_corpus import LegalCorpus, get_default_legal_corpus
from app.consumer.legal_policy import provision_is_eligible
from app.consumer.schemas import ProvisionStatus
from app.core.hashing import sha256_hex
from app.evaluation import label_ranks
from app.evaluation.consumer_runner import normalize_consumer_retrieval_hit
from app.schemas.evaluation import ConsumerLegalRelevance
from app.schemas.rag import Chunk, RetrievedChunk

FIRST = "A loja só me vendeu o celular se eu levasse a capinha junto."
SECOND = "O banco disse que só liberava o empréstimo se eu fizesse um seguro."


@lru_cache(maxsize=1)
def _base() -> LegalCorpus:
    return LegalCorpus(get_default_legal_corpus().provisions)


def _entry(
    provision_id: str,
    unit_key: str,
    *,
    status: str = "generated",
    source_sha256: str | None = None,
) -> AliasEntry:
    base = _base()
    provision = base.get(provision_id)
    unit = next((item for item in provision.units if item.unit_id == unit_key), None)
    return AliasEntry(
        unit_key=unit_key,
        provision_id=provision_id,
        source_sha256=source_sha256 or sha256_hex(base.alias_source_block(provision, unit)),
        aliases=(FIRST, SECOND),
        status=status,
    )


def _aliased(*entries: AliasEntry) -> LegalCorpus:
    return LegalCorpus(
        _base().provisions, aliases=AliasSet(prompt_version="test-prompt", entries=entries)
    )


def _aliases_of(corpus: LegalCorpus, **options: bool) -> list[Chunk]:
    return [
        chunk
        for chunk in corpus.as_chunks(**options)
        if chunk.metadata.get("chunk_level") == "alias"
    ]


def test_an_aliased_unit_gets_one_alias_chunk() -> None:
    corpus = _aliased(
        _entry("br-cdc-art-39", "br-cdc-art-39-inciso-i"),
        _entry("br-cf-art-5-xxxii", "br-cf-art-5-xxxii"),
    )

    # Sorted by id: the CDC unit's alias before the Constitution provision's.
    [unit_alias, provision_alias] = sorted(_aliases_of(corpus), key=lambda chunk: chunk.chunk_id)

    assert len(corpus.as_chunks()) == len(_base().as_chunks()) + 2
    assert unit_alias.chunk_id == f"{corpus.document_id}:legal:br-cdc-art-39-inciso-i:alias-01"
    assert unit_alias.text == f"{FIRST}\n{SECOND}"
    assert unit_alias.metadata["unit_id"] == "br-cdc-art-39-inciso-i"
    assert unit_alias.metadata["provision_id"] == "br-cdc-art-39"
    assert unit_alias.metadata["content_kind"] == "lay_alias"
    page = [item.provision_id for item in corpus.provisions].index("br-cdc-art-39") + 1
    assert (unit_alias.page_start, unit_alias.page_end) == (page, page)
    assert corpus.unit_for_chunk(unit_alias).unit_id == "br-cdc-art-39-inciso-i"  # type: ignore[union-attr]
    assert provision_alias.metadata["unit_id"] is None
    assert provision_alias.chunk_id.endswith(":legal:br-cf-art-5-xxxii:alias-01")


def test_rejected_inactive_and_uncitable_entries_make_no_chunk() -> None:
    base = _base()
    inactive_provision, inactive_unit = next(
        (provision, unit)
        for provision in base.provisions
        if provision.law_id == "br-cdc" and provision_is_eligible(provision)
        for unit in provision.units
        if unit.status is not ProvisionStatus.ACTIVE
    )
    uncitable = next(
        provision
        for provision in base.provisions
        if provision.law_id == "br-cdc" and provision.units and not provision_is_eligible(provision)
    )
    corpus = _aliased(
        _entry("br-cdc-art-39", "br-cdc-art-39-inciso-i", status="rejected"),
        _entry(inactive_provision.provision_id, inactive_unit.unit_id),
        _entry(uncitable.provision_id, uncitable.units[0].unit_id),
    )

    assert _aliases_of(corpus) == []
    assert _aliases_of(corpus, include_inactive=True, include_uncitable=True) == []


def test_entries_outside_coverage_or_the_statute_are_refused() -> None:
    civil = next(
        provision for provision in _base().provisions if provision.law_id == "br-cc" and provision.units
    )
    with pytest.raises(ValueError, match="br-cc is outside alias coverage"):
        _aliased(_entry(civil.provision_id, civil.units[0].unit_id))
    unknown = _entry("br-cdc-art-39", "br-cdc-art-39-inciso-i").model_copy(
        update={"unit_key": "br-cdc-art-39-inciso-xcix"}
    )
    with pytest.raises(ValueError, match="br-cdc-art-39-inciso-xcix: not a unit of br-cdc-art-39"):
        _aliased(unknown)


def test_a_stale_entry_fails_corpus_load_naming_it_and_the_fix() -> None:
    stale = _entry("br-cdc-art-39", "br-cdc-art-39-inciso-i", source_sha256="0" * 64)

    with pytest.raises(ValueError, match=r"br-cdc-art-39-inciso-i.*generate_aliases"):
        _aliased(stale)


def test_the_corpus_hash_covers_aliases_but_an_empty_set_changes_nothing() -> None:
    base = _base()
    empty = LegalCorpus(base.provisions, aliases=AliasSet(prompt_version="test-prompt"))
    generated = _aliased(_entry("br-cdc-art-39", "br-cdc-art-39-inciso-i"))
    reviewed = _aliased(_entry("br-cdc-art-39", "br-cdc-art-39-inciso-i", status="reviewed"))

    other_prompt = LegalCorpus(
        base.provisions,
        aliases=AliasSet(
            prompt_version="other-prompt",
            entries=(_entry("br-cdc-art-39", "br-cdc-art-39-inciso-i"),),
        ),
    )

    assert empty.corpus_sha256 == base.corpus_sha256
    assert generated.corpus_sha256 != base.corpus_sha256
    assert reviewed.corpus_sha256 != generated.corpus_sha256
    assert other_prompt.corpus_sha256 != generated.corpus_sha256
    assert get_default_legal_corpus().aliases is not None


def test_evaluators_count_an_alias_hit_as_its_units_hit() -> None:
    corpus = _aliased(_entry("br-cdc-art-39", "br-cdc-art-39-inciso-i"))
    [alias] = _aliases_of(corpus)
    hit = RetrievedChunk(chunk=alias, score=0.03)
    label = ConsumerLegalRelevance(
        article_id="br-cdc-art-39", unit_id="br-cdc-art-39-inciso-i", grade=3, rationale="r"
    )

    normalized = normalize_consumer_retrieval_hit(hit)

    assert (normalized.provision_id, normalized.unit_id) == (
        "br-cdc-art-39",
        "br-cdc-art-39-inciso-i",
    )
    assert label_ranks._matches(hit, label)
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `.venv/Scripts/python.exe -m pytest tests/test_consumer_alias_chunks.py -q -p no:cacheprovider`
Expected: FAIL — `LegalCorpus.__init__() got an unexpected keyword argument 'aliases'` / no `alias_source_block`.

- [ ] **Step 3: Implement alias support in `LegalCorpus`**

In `app/consumer/legal_corpus.py`:

1. Import: `from app.consumer.aliases import ALIAS_LAW_IDS, INDEXED_ALIAS_STATUSES, AliasEntry, AliasSet, load_alias_set` (keep ruff's import order).

2. Change the constructor signature to `def __init__(self, provisions: Sequence[LegalProvision], *, aliases: AliasSet | None = None) -> None:` and, right after the `self._by_id = MappingProxyType(...)` assignment, add:

```python
        self._aliases = aliases
        self._alias_entries = self._attach_aliases(aliases)
```

3. Below the `corpus_sha256` property add:

```python
    @property
    def aliases(self) -> AliasSet | None:
        """The lay-language alias file this corpus indexes, if any (ADR 0022)."""
        return self._aliases
```

4. In `_calculate_corpus_sha256`, immediately before `return canonical_json_sha256(payload)`:

```python
        if self._aliases is not None and self._aliases.entries:
            # An empty alias set keeps the identity of the releases before it.
            payload["schema_version"] = 3
            payload["aliases"] = self._aliases.identity()
```

5. In `as_chunks`, after the `chunks.extend(self._article_level_chunks(...))` call inside the provision loop, add:

```python
            chunks.extend(
                self._alias_chunks(
                    provision,
                    document_id=document_id,
                    page_number=page_number,
                    chunking_version=chunking_version,
                )
            )
```

6. Add these methods to `LegalCorpus` (next to `_article_level_chunks`):

```python
    @staticmethod
    def aliasable_units(provision: LegalProvision) -> tuple[LegalTextUnit | None, ...]:
        """The units that get unit chunks and may get aliases; ``(None,)`` without units."""

        if not provision.units:
            return (None,)
        return tuple(
            unit
            for unit in provision.units
            if unit.kind not in _AMENDMENT_KINDS and unit.status is ProvisionStatus.ACTIVE
        )

    def alias_source_block(self, provision: LegalProvision, unit: LegalTextUnit | None) -> str:
        """The text the alias generator sends for one unit; its hash pins the entry."""

        if unit is None:
            body = provision.official_text or provision.summary
            return f"[{provision.provision_id}] {provision.citation_label}:\n{body}"
        lead_in = self._lead_in_text(provision, unit)
        context = f"{lead_in}\n" if lead_in else ""
        return f"[{unit.unit_id}] {unit.label}:\n{context}{unit.text}"

    def _alias_chunks(
        self,
        provision: LegalProvision,
        *,
        document_id: str,
        page_number: int,
        chunking_version: str,
    ) -> list[Chunk]:
        """One chunk of lay paraphrases per aliased unit; it is never quoted."""

        if not self._alias_entries or not provision_is_eligible(provision):
            return []
        chunks: list[Chunk] = []
        for unit in self.aliasable_units(provision):
            key = provision.provision_id if unit is None else unit.unit_id
            entry = self._alias_entries.get(key)
            if entry is None:
                continue
            chunks.append(
                Chunk(
                    chunk_id=f"{document_id}:legal:{key}:alias-01",
                    doc_id=document_id,
                    text="\n".join(entry.aliases),
                    section=self._section_label(provision),
                    page_start=page_number,
                    page_end=page_number,
                    metadata=self._legal_metadata(
                        provision,
                        unit,
                        page=page_number,
                        chunking_version=chunking_version,
                        chunk_level="alias",
                        content_kind="lay_alias",
                    ),
                )
            )
        return chunks

    def _attach_aliases(self, aliases: AliasSet | None) -> Mapping[str, AliasEntry]:
        """Validate the alias file against this corpus; return the indexed entries."""

        if aliases is None:
            return MappingProxyType({})
        problems = [
            problem for problem in map(self._alias_problem, aliases.entries) if problem is not None
        ]
        if problems:
            raise ValueError("alias file does not match the corpus: " + "; ".join(problems[:10]))
        indexed = [entry for entry in aliases.entries if entry.status in INDEXED_ALIAS_STATUSES]
        stale = [
            entry.unit_key for entry in indexed if entry.source_sha256 != self._alias_sha256(entry)
        ]
        if stale:
            raise ValueError(
                f"{len(stale)} alias entries were generated from statute text that has since "
                f"changed ({', '.join(stale[:5])}); regenerate them with "
                "`python -m app.consumer.generate_aliases`"
            )
        return MappingProxyType({entry.unit_key: entry for entry in indexed})

    def _alias_problem(self, entry: AliasEntry) -> str | None:
        provision = self._by_id.get(entry.provision_id)
        if provision is None:
            return f"{entry.unit_key}: unknown provision {entry.provision_id}"
        if provision.law_id not in ALIAS_LAW_IDS:
            return f"{entry.unit_key}: {provision.law_id} is outside alias coverage"
        found, _ = self._alias_target(provision, entry.unit_key)
        if not found:
            return f"{entry.unit_key}: not a unit of {entry.provision_id}"
        return None

    @staticmethod
    def _alias_target(
        provision: LegalProvision, unit_key: str
    ) -> tuple[bool, LegalTextUnit | None]:
        if not provision.units:
            return unit_key == provision.provision_id, None
        unit = next((item for item in provision.units if item.unit_id == unit_key), None)
        return unit is not None, unit

    def _alias_sha256(self, entry: AliasEntry) -> str:
        provision = self._by_id[entry.provision_id]
        _, unit = self._alias_target(provision, entry.unit_key)
        return sha256_hex(self.alias_source_block(provision, unit))
```

7. In `unit_for_chunk`, replace the loop body condition with one that also recognises alias ids:

```python
        for unit in provision.units:
            unit_ids = (f":legal:{unit.unit_id}:part-", f":legal:{unit.unit_id}:alias-")
            if any(marker in chunk.chunk_id for marker in unit_ids):
                return unit
        return None
```

8. In `_legal_metadata`, add a keyword parameter `content_kind: str | None = None` after `lead_in_unit_ids` and replace its first line with:

```python
        content_kind = content_kind or (
            "official" if provision.official_text is not None or unit else "editorial"
        )
```

9. Rename `_default_provisions` to `default_legal_provisions` (definition and caller), and make the default corpus read the alias file:

```python
@lru_cache(maxsize=1)
def get_default_legal_corpus() -> LegalCorpus:
    return LegalCorpus(default_legal_provisions(), aliases=load_alias_set())
```

10. Create `app/consumer/data/aliases/aliases.json`:

```json
{
  "schema_version": 1,
  "review_status": "requires_legal_review",
  "prompt_version": "consumer-lay-aliases:v1",
  "prompt_sha256": null,
  "model": null,
  "generated_on": null,
  "entries": []
}
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `.venv/Scripts/python.exe -m pytest tests/test_consumer_alias_chunks.py tests/test_consumer_legal_corpus.py tests/test_consumer_citation_integrity.py -q -p no:cacheprovider`
Expected: PASS (the empty alias file leaves every existing corpus test unchanged).

- [ ] **Step 5: Full suite, lint, type-check, commit**

```bash
.venv/Scripts/python.exe -m pytest -q -p no:cacheprovider --deselect tests/test_demo_manifest.py::test_demo_pdf_manifest_is_complete_current_and_reviewed
.venv/Scripts/python.exe -m ruff check app tests && .venv/Scripts/python.exe -m mypy app
git add app/consumer/legal_corpus.py app/consumer/data/aliases/aliases.json tests/test_consumer_alias_chunks.py
git commit -m "feat(consumer): index one alias chunk per aliased legal unit

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

Expected: the full suite passes unchanged; the default alias file is empty, so the corpus identity and every pinned measurement stay the same.

---

### Task 3: A citation found through an alias quotes official text

**Files:**
- Modify: `app/consumer/schemas.py` (`LegalAuthorityCitation.matched_chunk_id`, `from_provision`)
- Modify: `app/consumer/legal_corpus.py` (`authority_for_chunk`, `_canonical_chunk_for_citation` split, `_quoted_chunk`)
- Modify: `app/consumer/service.py` (`_cited_chunk_ids`)
- Test: `tests/test_consumer_alias_chunks.py`

**Interfaces:**
- Consumes: alias chunks and `aliasable_units` (Task 2).
- Produces: `LegalAuthorityCitation.matched_chunk_id: str | None`; `app.consumer.service._cited_chunk_ids(grounds: list[LegalGround]) -> set[str]`.

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_consumer_alias_chunks.py` (add `from app.consumer.schemas import LegalGround` and `from app.consumer.service import _cited_chunk_ids`):

```python
def test_a_citation_found_through_an_alias_quotes_the_official_text() -> None:
    corpus = _aliased(
        _entry("br-cdc-art-39", "br-cdc-art-39-inciso-i"),
        _entry("br-cf-art-5-xxxii", "br-cf-art-5-xxxii"),
    )
    by_key = {chunk.chunk_id.split(":legal:")[1]: chunk for chunk in _aliases_of(corpus)}
    unit_alias = by_key["br-cdc-art-39-inciso-i:alias-01"]

    authority = corpus.authority_for_chunk(RetrievedChunk(chunk=unit_alias, score=0.03))
    provision_authority = corpus.authority_for_chunk(
        RetrievedChunk(chunk=by_key["br-cf-art-5-xxxii:alias-01"], score=0.03)
    )

    unit = corpus.unit_for_chunk(unit_alias)
    assert unit is not None
    assert authority.chunk_id == unit_alias.chunk_id.replace(":alias-01", ":part-01")
    assert authority.matched_chunk_id == unit_alias.chunk_id
    assert authority.unit_id == "br-cdc-art-39-inciso-i"
    assert authority.official_excerpt is not None
    assert authority.official_excerpt in unit.text
    assert FIRST not in authority.official_excerpt
    assert provision_authority.chunk_id is not None
    assert provision_authority.chunk_id.endswith(":legal:br-cf-art-5-xxxii:part-01")


def test_an_official_hit_records_no_matched_alias() -> None:
    corpus = _aliased(_entry("br-cdc-art-39", "br-cdc-art-39-inciso-i"))
    official = next(
        chunk
        for chunk in corpus.as_chunks()
        if chunk.chunk_id.endswith(":legal:br-cdc-art-39-inciso-i:part-01")
    )

    authority = corpus.authority_for_chunk(RetrievedChunk(chunk=official, score=0.03))

    assert authority.matched_chunk_id is None


def test_the_trace_marks_the_alias_that_led_to_a_citation() -> None:
    corpus = _aliased(_entry("br-cdc-art-39", "br-cdc-art-39-inciso-i"))
    [alias] = _aliases_of(corpus)
    ground = LegalGround(
        authority=corpus.authority_for_chunk(RetrievedChunk(chunk=alias, score=0.03)),
        application_to_facts="fixture",
    )

    assert _cited_chunk_ids([ground]) == {
        alias.chunk_id,
        alias.chunk_id.replace(":alias-01", ":part-01"),
    }
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `.venv/Scripts/python.exe -m pytest tests/test_consumer_alias_chunks.py -q -p no:cacheprovider`
Expected: FAIL — `_cited_chunk_ids` cannot be imported (collection error); once stubbed, `canonical legal chunk has an invalid chunk level`.

- [ ] **Step 3: Add `matched_chunk_id` to the citation schema**

In `app/consumer/schemas.py`, `LegalAuthorityCitation`, after `chunk_id: str | None = None`:

```python
    # The retrieved chunk when it is not the quoted one: a lay-language alias
    # (ADR 0022) leads to a unit, whose official text is what gets quoted.
    matched_chunk_id: str | None = None
```

In `from_provision`, add the keyword parameter `matched_chunk_id: str | None = None` after `chunk_id` and pass `matched_chunk_id=matched_chunk_id,` after `chunk_id=chunk_id,` in the `cls(...)` call.

- [ ] **Step 4: Resolve alias chunks to official text**

In `app/consumer/legal_corpus.py`, replace `authority_for_chunk` with:

```python
    def authority_for_chunk(
        self,
        item: Chunk | RetrievedChunk,
        *,
        retrieval_rank: int | None = None,
    ) -> LegalAuthorityCitation:
        retrieved_chunk = _as_chunk(item)
        score = item.score if isinstance(item, RetrievedChunk) else None
        matched = self._canonical_chunk_for_citation(retrieved_chunk)
        chunk = self._quoted_chunk(matched)
        provision = self.provision_for_chunk(chunk)
        unit = self.unit_for_chunk(chunk)
        official_excerpt = self._canonical_chunk_body(chunk, provision, unit)
        return LegalAuthorityCitation.from_provision(
            provision,
            unit=unit,
            official_excerpt=official_excerpt,
            official_excerpt_sha256=sha256_hex(official_excerpt),
            chunk_id=chunk.chunk_id,
            matched_chunk_id=matched.chunk_id if matched.chunk_id != chunk.chunk_id else None,
            retrieval_rank=retrieval_rank,
            retrieval_score=score,
        )
```

Replace `_canonical_chunk_for_citation` (keep its docstring) with:

```python
    def _canonical_chunk_for_citation(self, chunk: Chunk) -> Chunk:
        """Resolve a store result by stable id and reject altered payloads.

        Empty metadata is tolerated for legacy stores because the stable id is
        sufficient to reconstruct it.  Any metadata that is present, however,
        must match the canonical corpus in full.
        """

        canonical = self._canonical_chunks(self._target_chars(chunk)).get(chunk.chunk_id)
        if canonical is None:
            raise ValueError("legal chunk id does not resolve to the canonical corpus")
        if chunk.model_dump(exclude={"metadata"}) != canonical.model_dump(
            exclude={"metadata"}
        ):
            raise ValueError("legal chunk does not match its canonical corpus identity")
        if chunk.metadata and chunk.metadata != canonical.metadata:
            raise ValueError("legal chunk metadata does not match the canonical corpus")
        return canonical

    def _quoted_chunk(self, chunk: Chunk) -> Chunk:
        """The chunk a citation quotes: an alias resolves to its unit's first official part."""

        if chunk.metadata.get("chunk_level") != "alias":
            return chunk
        key = chunk.metadata.get("unit_id") or chunk.metadata["provision_id"]
        official_id = f"{chunk.doc_id}:legal:{key}:part-01"
        return self._canonical_chunks(self._target_chars(chunk))[official_id]

    @staticmethod
    def _target_chars(chunk: Chunk) -> int:
        raw_chunking_version = chunk.metadata.get("chunking_version")
        version_prefix = f"{LEGAL_CHUNKING_VERSION}:target="
        if isinstance(raw_chunking_version, str) and raw_chunking_version.startswith(
            version_prefix
        ):
            try:
                return int(raw_chunking_version.removeprefix(version_prefix))
            except ValueError as exc:
                raise ValueError("legal chunk has an invalid chunking identity") from exc
        return DEFAULT_LEGAL_CHUNK_TARGET_CHARS

    def _canonical_chunks(self, target_chars: int) -> Mapping[str, Chunk]:
        canonical_chunks = self._canonical_chunk_maps.get(target_chars)
        if canonical_chunks is None:
            generated = self.as_chunks(target_chars=target_chars)
            canonical_chunks = MappingProxyType({item.chunk_id: item for item in generated})
            if len(canonical_chunks) != len(generated):  # pragma: no cover - corpus invariant
                raise ValueError("canonical legal corpus contains duplicate chunk ids")
            self._canonical_chunk_maps[target_chars] = canonical_chunks
        return canonical_chunks
```

- [ ] **Step 5: Mark the matched alias in the notice trace**

In `app/consumer/service.py`, add near `_annotate_composer_selection`:

```python
def _cited_chunk_ids(grounds: list[LegalGround]) -> set[str]:
    """Chunks that became citations: the quoted one and the alias that matched it."""
    return {
        chunk_id
        for ground in grounds
        for chunk_id in (ground.authority.chunk_id, ground.authority.matched_chunk_id)
        if chunk_id is not None
    }
```

and in `_generate_notice` replace the set passed to `_annotate_composer_selection` for legal traces:

```python
        legal_traces = _annotate_composer_selection(
            legal_traces,
            _merge_results(legal_results),
            _cited_chunk_ids(legal_grounds),
        )
```

(`LegalGround` is already imported in `service.py`; add it to the import if not.)

- [ ] **Step 6: Run the tests to verify they pass**

Run: `.venv/Scripts/python.exe -m pytest tests/test_consumer_alias_chunks.py tests/test_consumer_citation_integrity.py tests/test_consumer_legal_corpus.py tests/test_consumer_api.py -q -p no:cacheprovider`
Expected: PASS.

- [ ] **Step 7: Lint, type-check, commit**

```bash
.venv/Scripts/python.exe -m ruff check app tests && .venv/Scripts/python.exe -m mypy app
git add app/consumer/schemas.py app/consumer/legal_corpus.py app/consumer/service.py tests/test_consumer_alias_chunks.py
git commit -m "feat(consumer): quote official text for grounds found through an alias

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

---

### Task 4: The offline alias generator

**Files:**
- Create: `app/consumer/alias_generation.py`, `app/consumer/generate_aliases.py`
- Modify: `pyproject.toml` (coverage source)
- Test: `tests/test_alias_generation.py`

**Interfaces:**
- Consumes: `AliasEntry`, `AliasSet`, `alias_problem`, `normalize_alias`, `load_alias_set`, `write_alias_set`, `ALIASES_PATH`, `ALIAS_LAW_IDS` (Task 1); `LegalCorpus.aliasable_units`, `alias_source_block`, `retrievable_provisions`, `default_legal_provisions` (Task 2); `LLMClient.parse` and `create_llm_client` (`app.llm`).
- Produces, in `app.consumer.alias_generation`: `ALIAS_PROMPT_VERSION = "consumer-lay-aliases:v1"`, `SYSTEM_PROMPT`, `USER_TEMPLATE`, `PROMPT_SHA256`, `AliasRequest(provision_id, user, sources: Mapping[str, str])`, `alias_requests(corpus, *, articles=()) -> list[AliasRequest]`, `GenerationReport`, `generate_aliases(corpus, alias_set, client, *, model, today, save, articles=(), force=False) -> tuple[AliasSet, GenerationReport]`, `render_report(report) -> str`, `main(argv=None) -> int` (async). CLI: `python -m app.consumer.generate_aliases [--model M] [--article ID]... [--force] [--dry-run] [--output PATH]`.

- [ ] **Step 1: Write the failing tests**

Create `tests/test_alias_generation.py`:

```python
"""The offline alias generator: requests, resume, protection and the CLI."""

from __future__ import annotations

import re
from datetime import date
from functools import lru_cache
from pathlib import Path
from typing import Any

import pytest
from pydantic import BaseModel

from app.consumer import alias_generation
from app.consumer.alias_generation import (
    ALIAS_PROMPT_VERSION,
    PROMPT_SHA256,
    AliasRequest,
    alias_requests,
    generate_aliases,
    render_report,
)
from app.consumer.aliases import AliasSet, load_alias_set
from app.consumer.legal_corpus import LegalCorpus, default_legal_provisions
from app.core.hashing import sha256_hex
from app.evaluation.consumer_golden import load_consumer_legal_dataset
from app.llm.base import LLMCallMetadata, ParsedResult

DATASET_PATH = Path("eval_data/consumer_legal_retrieval")
TODAY = date(2026, 10, 3)
_KEY = re.compile(r"^\[([a-z0-9-]+)\]", re.MULTILINE)


@lru_cache(maxsize=1)
def _corpus() -> LegalCorpus:
    return LegalCorpus(default_legal_provisions())


class _FakeLLM:
    """Answers every requested unit with three valid aliases, unless told otherwise."""

    def __init__(
        self,
        *,
        overrides: dict[str, list[str]] | None = None,
        extra: list[dict[str, Any]] | None = None,
        fail_on: str | None = None,
    ) -> None:
        self.calls: list[str] = []
        self._overrides = overrides or {}
        self._extra = extra or []
        self._fail_on = fail_on
        self._counter = 0

    async def complete(self, **_: Any) -> Any:  # pragma: no cover - never called
        raise AssertionError("the generator must use parse")

    async def parse(self, *, user: str, schema: type[BaseModel], **_: Any) -> ParsedResult[Any]:
        self.calls.append(user)
        if self._fail_on is not None and self._fail_on in user:
            raise RuntimeError("provider down")
        units = [
            {"unit_key": key, "aliases": self._overrides.get(key, self._aliases())}
            for key in _KEY.findall(user)
        ]
        return ParsedResult(
            data=schema.model_validate({"units": [*units, *self._extra]}),
            meta=LLMCallMetadata(provider="fake", model="fake", latency_ms=0.0),
        )

    def _aliases(self) -> list[str]:
        self._counter += 1
        return [f"Situação de consumo número {self._counter}-{index} que vivi." for index in (1, 2, 3)]


def _empty() -> AliasSet:
    return AliasSet(prompt_version=ALIAS_PROMPT_VERSION)


async def _run(
    llm: _FakeLLM, alias_set: AliasSet, *articles: str, force: bool = False
) -> tuple[AliasSet, Any, list[AliasSet]]:
    saved: list[AliasSet] = []
    result, report = await generate_aliases(
        _corpus(),
        alias_set,
        llm,  # type: ignore[arg-type]
        model="fake-model",
        today=TODAY,
        save=saved.append,
        articles=articles,
        force=force,
    )
    return result, report, saved


def test_one_request_per_covered_article_with_lead_ins() -> None:
    requests = alias_requests(_corpus())
    [article_39] = alias_requests(_corpus(), articles=["br-cdc-art-39"])
    provision = _corpus().get("br-cdc-art-39")

    assert len(requests) == 108
    assert {request.provision_id.split("-art-")[0] for request in requests} == {
        "br-cdc",
        "br-lgpd",
        "br-cf",
    }
    assert provision.citation_label in article_39.user
    assert "br-cdc-art-39-inciso-i" in article_39.sources
    caput = next(unit for unit in provision.units if unit.kind.value == "caput")
    assert caput.text in article_39.sources["br-cdc-art-39-inciso-i"]


def test_no_request_carries_golden_text() -> None:
    dataset = load_consumer_legal_dataset(DATASET_PATH)
    prompts = [request.user for request in alias_requests(_corpus())]

    for case in dataset.cases:
        assert not any(case.complaint in prompt for prompt in prompts), case.case_id
        assert not any(case.desired_resolution in prompt for prompt in prompts), case.case_id


def test_unknown_or_uncovered_articles_are_refused() -> None:
    civil = next(
        provision.provision_id
        for provision in _corpus().retrievable_provisions()
        if provision.law_id == "br-cc"
    )
    with pytest.raises(ValueError, match="not a covered, citable article: br-cdc-art-999"):
        alias_requests(_corpus(), articles=["br-cdc-art-999"])
    with pytest.raises(ValueError, match=civil):
        alias_requests(_corpus(), articles=[civil])


async def test_generation_writes_entries_and_saves_after_each_article() -> None:
    llm = _FakeLLM()

    result, report, saved = await _run(llm, _empty(), "br-cdc-art-39", "br-cf-art-5-xxxii")

    # Corpus order: the Constitution's provisions come before the CDC's.
    [constitution, article_39] = alias_requests(
        _corpus(), articles=["br-cdc-art-39", "br-cf-art-5-xxxii"]
    )
    assert len(llm.calls) == 2
    assert len(saved) == 2
    assert {entry.unit_key for entry in result.entries} == {
        *article_39.sources,
        *constitution.sources,
    }
    entry = next(item for item in result.entries if item.unit_key == "br-cdc-art-39-inciso-i")
    assert entry.source_sha256 == sha256_hex(article_39.sources["br-cdc-art-39-inciso-i"])
    assert (result.model, result.generated_on, result.prompt_sha256) == (
        "fake-model",
        TODAY,
        PROMPT_SHA256,
    )
    assert report.written and not report.missing and not report.short
    # The generated file is accepted by the corpus as is.
    LegalCorpus(_corpus().provisions, aliases=result)


async def test_a_rerun_skips_current_entries_and_never_touches_protected_ones() -> None:
    first, _, _ = await _run(_FakeLLM(), _empty(), "br-cdc-art-39")
    key = "br-cdc-art-39-inciso-i"
    protected = tuple(
        entry.model_copy(update={"status": "reviewed", "source_sha256": "0" * 64})
        if entry.unit_key == key
        else entry
        for entry in first.entries
    )
    edited = first.model_copy(update={"entries": protected})

    rerun = _FakeLLM()
    second, report, saved = await _run(rerun, edited, "br-cdc-art-39")
    forced, _, _ = await _run(_FakeLLM(), edited, "br-cdc-art-39", force=True)

    assert rerun.calls == []
    assert saved == []
    assert report.skipped == len(first.entries)
    assert next(e for e in second.entries if e.unit_key == key).status == "reviewed"
    assert next(e for e in forced.entries if e.unit_key == key).status == "generated"


async def test_invalid_aliases_are_dropped_and_short_or_missing_units_reported() -> None:
    legal = "Isso está no art. 39 e a loja sabia disso."
    valid = ["Comprei um celular e me obrigaram a levar a capinha.", "Fui obrigado a comprar dois itens juntos."]
    llm = _FakeLLM(
        overrides={
            "br-cdc-art-39-inciso-i": [legal, *valid],
            "br-cdc-art-39-inciso-ii": [legal, valid[0]],
        },
        extra=[
            {"unit_key": "br-cdc-art-40-caput", "aliases": valid},
            {"unit_key": "br-cdc-art-39-inciso-i", "aliases": ["Outra resposta duplicada qualquer.", *valid]},
        ],
    )

    result, report, _ = await _run(llm, _empty(), "br-cdc-art-39")

    entry = next(item for item in result.entries if item.unit_key == "br-cdc-art-39-inciso-i")
    assert entry.aliases == tuple(valid)
    assert "br-cdc-art-39-inciso-ii" in report.short
    assert report.dropped == 2
    assert all(item.unit_key.startswith("br-cdc-art-39") for item in result.entries)
    assert "fewer than two valid aliases" in render_report(report)


async def test_a_missing_answer_is_reported() -> None:
    llm = _FakeLLM()

    async def silent(**kwargs: Any) -> ParsedResult[Any]:
        result = await _FakeLLM.parse(llm, **kwargs)
        units = [unit for unit in result.data.units if unit.unit_key != "br-cdc-art-39-inciso-i"]
        return ParsedResult(data=result.data.model_copy(update={"units": units}), meta=result.meta)

    llm.parse = silent  # type: ignore[method-assign]
    _, report, _ = await _run(llm, _empty(), "br-cdc-art-39")

    assert report.missing == ["br-cdc-art-39-inciso-i"]
    assert "no answer: br-cdc-art-39-inciso-i" in render_report(report)


async def test_a_failure_keeps_finished_articles_and_a_rerun_resumes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    output = tmp_path / "aliases.json"
    articles = ("--article", "br-cdc-art-39", "--article", "br-cf-art-5-xxxii")

    # The Constitution article runs first and is saved; the CDC one fails.
    code = await _cli(monkeypatch, _FakeLLM(fail_on="br-cdc-art-39"), output, *articles)
    resumed = _FakeLLM()
    second = await _cli(monkeypatch, resumed, output, *articles)

    assert code == 1
    assert "rerun to resume" in capsys.readouterr().err
    assert second == 0
    assert len(resumed.calls) == 1
    assert "br-cdc-art-39" in resumed.calls[0]
    assert {entry.provision_id for entry in load_alias_set(output).entries} == {
        "br-cdc-art-39",
        "br-cf-art-5-xxxii",
    }


async def _cli(
    monkeypatch: pytest.MonkeyPatch, llm: _FakeLLM, output: Path, *arguments: str
) -> int:
    monkeypatch.setattr(alias_generation, "create_llm_client", lambda _settings: llm)
    return await alias_generation.main(
        ["--model", "test-model", "--output", str(output), *arguments]
    )


async def test_cli_dry_run_writes_nothing_and_refuses_unknown_articles(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    output = tmp_path / "aliases.json"
    monkeypatch.setattr(alias_generation, "create_llm_client", lambda _settings: pytest.fail("no LLM"))

    assert await alias_generation.main(["--dry-run", "--article", "br-cdc-art-39", "--output", str(output)]) == 0
    assert "--- br-cdc-art-39" in capsys.readouterr().out
    assert not output.exists()
    with pytest.raises(SystemExit) as excinfo:
        await alias_generation.main(["--article", "br-cdc-art-999", "--output", str(output)])
    assert excinfo.value.code == 2


async def test_cli_reports_and_writes_the_file(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    output = tmp_path / "aliases.json"
    llm = _FakeLLM()
    monkeypatch.setattr(alias_generation, "create_llm_client", lambda _settings: llm)

    code = await alias_generation.main(
        ["--model", "test-model", "--article", "br-cf-art-5-xxxii", "--output", str(output)]
    )

    assert code == 0
    assert load_alias_set(output).model == "test-model"
    assert "written: 1" in capsys.readouterr().out
```

Wrap any test line ruff reports over 100 characters.

- [ ] **Step 2: Run the tests to verify they fail**

Run: `.venv/Scripts/python.exe -m pytest tests/test_alias_generation.py -q -p no:cacheprovider`
Expected: FAIL — `ModuleNotFoundError: No module named 'app.consumer.alias_generation'`.

- [ ] **Step 3: Create `app/consumer/alias_generation.py`**

```python
"""Offline generation of lay-language aliases (ADR 0022).

One LLM call per covered article sends the citation label, the hierarchy and
each in-force unit with its lead-in, and asks for two to four sentences per
unit in a lay consumer's words. The generator reads the corpus only: it never
sees the golden set, so the holdout measures aliases written without it.

The run is resumable. The alias file is rewritten after every article; a rerun
skips units whose entry matches the current statute text and prompt version,
and never replaces a reviewed or rejected entry unless forced.
"""

from __future__ import annotations

import argparse
import sys
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path
from types import MappingProxyType

from pydantic import BaseModel, Field

from app.consumer.aliases import (
    ALIAS_LAW_IDS,
    ALIASES_PATH,
    AliasEntry,
    AliasSet,
    alias_problem,
    load_alias_set,
    normalize_alias,
    write_alias_set,
)
from app.consumer.legal_corpus import LegalCorpus, default_legal_provisions
from app.consumer.statutes import HIERARCHY_LEVELS
from app.core.config import Settings
from app.core.hashing import sha256_hex
from app.llm.base import LLMClient
from app.llm.factory import create_llm_client

ALIAS_PROMPT_VERSION = "consumer-lay-aliases:v1"
SYSTEM_PROMPT = (
    "Você escreve paráfrases leigas para um índice de busca de direitos do consumidor no "
    "Brasil. Para cada unidade de um dispositivo legal, indicada pela chave entre colchetes, "
    "escreva de 2 a 4 frases curtas, em primeira pessoa, como um consumidor comum descreveria "
    "uma situação concreta coberta por aquela unidade.\n"
    "Regras:\n"
    "- Cada frase é específica da sua unidade e diferente das frases das outras unidades do "
    "mesmo dispositivo.\n"
    "- Não cite artigos, incisos, parágrafos, alíneas, leis, códigos ou siglas como CDC ou "
    "LGPD.\n"
    "- Prefira palavras do dia a dia a termos jurídicos.\n"
    "- No máximo 25 palavras por frase.\n"
    "- Não atribua à unidade direitos que o texto não dá.\n"
    "- Responda com uma entrada por unidade, usando exatamente a chave entre colchetes."
)
USER_TEMPLATE = "Dispositivo: {citation}\nHierarquia: {hierarchy}\n\nUnidades:\n\n{blocks}"
PROMPT_SHA256 = sha256_hex(f"{SYSTEM_PROMPT}\n{USER_TEMPLATE}")
MAX_ALIASES = 4


class _UnitAliases(BaseModel):
    unit_key: str
    aliases: list[str] = Field(default_factory=list)


class _ArticleAliases(BaseModel):
    units: list[_UnitAliases] = Field(default_factory=list)


@dataclass(frozen=True, slots=True)
class AliasRequest:
    """One article's request: the prompt and the source block of each unit."""

    provision_id: str
    user: str
    sources: Mapping[str, str]


@dataclass(slots=True)
class GenerationReport:
    written: list[str] = field(default_factory=list)
    skipped: int = 0
    dropped: int = 0
    missing: list[str] = field(default_factory=list)
    short: list[str] = field(default_factory=list)


def alias_requests(corpus: LegalCorpus, *, articles: Sequence[str] = ()) -> list[AliasRequest]:
    """The covered, citable articles in corpus order, or only ``articles``."""

    wanted = set(articles)
    requests: list[AliasRequest] = []
    for provision in corpus.retrievable_provisions():
        if provision.law_id not in ALIAS_LAW_IDS:
            continue
        if wanted and provision.provision_id not in wanted:
            continue
        sources = {
            provision.provision_id if unit is None else unit.unit_id: corpus.alias_source_block(
                provision, unit
            )
            for unit in corpus.aliasable_units(provision)
        }
        if not sources:  # every unit vetoed or revoked: nothing to alias
            continue
        levels = (getattr(provision, name) for name in HIERARCHY_LEVELS)
        hierarchy = " > ".join(str(level) for level in levels if level)
        user = USER_TEMPLATE.format(
            citation=provision.citation_label,
            hierarchy=hierarchy or "-",
            blocks="\n\n".join(sources.values()),
        )
        requests.append(AliasRequest(provision.provision_id, user, MappingProxyType(sources)))
    unknown = sorted(wanted - {request.provision_id for request in requests})
    if unknown:
        raise ValueError("not a covered, citable article: " + ", ".join(unknown))
    return requests


async def generate_aliases(
    corpus: LegalCorpus,
    alias_set: AliasSet,
    client: LLMClient,
    *,
    model: str,
    today: date,
    save: Callable[[AliasSet], None],
    articles: Sequence[str] = (),
    force: bool = False,
) -> tuple[AliasSet, GenerationReport]:
    """Generate the missing or stale entries, saving after each article."""

    entries = {entry.unit_key: entry for entry in alias_set.entries}
    order = {key: index for index, key in enumerate(_corpus_order(corpus))}
    stale_prompt = alias_set.prompt_version != ALIAS_PROMPT_VERSION
    report = GenerationReport()
    current = alias_set
    for request in alias_requests(corpus, articles=articles):
        pending = [
            key
            for key, source in request.sources.items()
            if _needs_generation(entries.get(key), source, force=force, stale_prompt=stale_prompt)
        ]
        report.skipped += len(request.sources) - len(pending)
        if not pending:
            continue
        result = await client.parse(
            system=SYSTEM_PROMPT,
            user=request.user,
            schema=_ArticleAliases,
            temperature=0.0,
            prompt_version=ALIAS_PROMPT_VERSION,
        )
        entries.update(_new_entries(request, pending, result.data, report))
        current = AliasSet(
            prompt_version=ALIAS_PROMPT_VERSION,
            prompt_sha256=PROMPT_SHA256,
            model=model,
            generated_on=today,
            entries=tuple(
                sorted(entries.values(), key=lambda entry: order.get(entry.unit_key, len(order)))
            ),
        )
        save(current)
    return current, report


def render_report(report: GenerationReport) -> str:
    lines = [
        f"written: {len(report.written)}",
        f"skipped (current or protected): {report.skipped}",
        f"dropped invalid aliases: {report.dropped}",
    ]
    if report.missing:
        lines.append("no answer: " + ", ".join(report.missing))
    if report.short:
        lines.append("fewer than two valid aliases: " + ", ".join(report.short))
    return "\n".join(lines)


def _needs_generation(
    entry: AliasEntry | None, source: str, *, force: bool, stale_prompt: bool
) -> bool:
    if entry is None or force:
        return True
    if entry.status != "generated":
        return False
    return stale_prompt or entry.source_sha256 != sha256_hex(source)


def _new_entries(
    request: AliasRequest,
    pending: list[str],
    answer: _ArticleAliases,
    report: GenerationReport,
) -> dict[str, AliasEntry]:
    answers: dict[str, list[str]] = {}
    for unit in answer.units:
        # Units nobody asked about are ignored, and the first answer wins.
        if unit.unit_key in pending and unit.unit_key not in answers:
            answers[unit.unit_key] = unit.aliases
    entries: dict[str, AliasEntry] = {}
    for key in pending:
        if key not in answers:
            report.missing.append(key)
            continue
        aliases = _valid_aliases(answers[key], report)
        if len(aliases) < 2:
            report.short.append(key)
            continue
        entries[key] = AliasEntry(
            unit_key=key,
            provision_id=request.provision_id,
            source_sha256=sha256_hex(request.sources[key]),
            aliases=tuple(aliases[:MAX_ALIASES]),
        )
        report.written.append(key)
    return entries


def _valid_aliases(raw: list[str], report: GenerationReport) -> list[str]:
    kept: list[str] = []
    for text in (normalize_alias(item) for item in raw):
        if alias_problem(text) or text.casefold() in {alias.casefold() for alias in kept}:
            report.dropped += 1
            continue
        kept.append(text)
    return kept


def _corpus_order(corpus: LegalCorpus) -> list[str]:
    return [key for request in alias_requests(corpus) for key in request.sources]


async def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Generate lay-language aliases for the citable CDC, LGPD and CF units."
    )
    parser.add_argument("--model", help="LLM model (default: LITIGATION_LLM_MODEL)")
    parser.add_argument(
        "--article", action="append", default=[], metavar="PROVISION_ID", help="repeatable"
    )
    parser.add_argument(
        "--force", action="store_true", help="also regenerate reviewed and rejected entries"
    )
    parser.add_argument(
        "--dry-run", action="store_true", help="print the requests; call no model, write nothing"
    )
    parser.add_argument("--output", type=Path, default=ALIASES_PATH)
    args = parser.parse_args(argv)
    corpus = LegalCorpus(default_legal_provisions())
    try:
        requests = alias_requests(corpus, articles=args.article)
    except ValueError as exc:
        parser.error(str(exc))
    if args.dry_run:
        for request in requests:
            print(f"--- {request.provision_id} ({len(request.sources)} units)\n{request.user}\n")
        return 0
    alias_set = (
        load_alias_set(args.output)
        if args.output.exists()
        else AliasSet(prompt_version=ALIAS_PROMPT_VERSION)
    )
    settings = Settings(llm_model=args.model) if args.model else Settings()
    model = settings.llm_model or settings.llm_provider.value
    try:
        _, report = await generate_aliases(
            corpus,
            alias_set,
            create_llm_client(settings),
            model=model,
            today=date.today(),
            save=lambda current: write_alias_set(current, args.output),
            articles=args.article,
            force=args.force,
        )
    except Exception as exc:  # the file already holds every finished article
        print(
            f"generate_aliases: stopped ({type(exc).__name__}: {exc}); rerun to resume",
            file=sys.stderr,
        )
        return 1
    print(render_report(report))
    return 0
```

Create `app/consumer/generate_aliases.py`:

```python
"""``python -m app.consumer.generate_aliases``: see ``app.consumer.alias_generation``."""

import asyncio

from app.consumer.alias_generation import main

if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(asyncio.run(main()))
```

In `pyproject.toml`, add `"app.consumer.alias_generation",` to the coverage source.

- [ ] **Step 4: Run the tests to verify they pass**

Run: `.venv/Scripts/python.exe -m pytest tests/test_alias_generation.py -q -p no:cacheprovider --cov=app.consumer.alias_generation --cov-branch --cov-report=term-missing`
Expected: PASS, `app/consumer/alias_generation.py` 100%.

- [ ] **Step 5: Lint, type-check, commit**

```bash
.venv/Scripts/python.exe -m ruff check app tests && .venv/Scripts/python.exe -m mypy app && .venv/Scripts/vulture app --min-confidence 90
git add app/consumer/alias_generation.py app/consumer/generate_aliases.py pyproject.toml tests/test_alias_generation.py
git commit -m "feat(consumer): offline lay-language alias generator

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

---

### Task 5: Generate the aliases and release corpus v5

**Files:**
- Modify: `app/consumer/data/aliases/aliases.json` (generated)
- Modify: `app/consumer/legal_corpus.py` (`CONSUMER_LAW_CORPUS_RELEASE_ID`, `LEGAL_CHUNKING_VERSION`)
- Modify: `eval_data/consumer_legal_retrieval/dataset.json` (2.3.0)
- Modify: `app/evaluation/label_ranks.py` (`DEFAULT_DEPTH` comment)
- Modify: `.github/workflows/ci.yml` (re-baselined development gates)
- Test: `tests/test_consumer_legal_corpus.py`, `tests/test_consumer_evaluation.py`, `tests/test_consumer_notice_evaluation.py`, `tests/test_label_ranks.py` and any other test that pins a measurement of the default corpus

**Interfaces:**
- Consumes: the generator CLI (Task 4), alias chunks and citations (Tasks 2–3), `compare` (ADR 0021).
- Produces: a populated alias file; corpus release v5; dataset 2.3.0; re-baselined pins and CI gates; `$WS/compare-dev.txt` and `$WS/compare-holdout.txt` for Task 6.

- [ ] **Step 1: Record the v4 offline baseline**

```bash
mkdir -p "$WS"
for split in development holdout; do
  .venv/Scripts/python.exe -m app.evaluation.consumer_runner eval_data/consumer_legal_retrieval --evaluate-notice --split $split --output "$WS/v4-notice-$split.json"
  .venv/Scripts/python.exe -m app.evaluation.consumer_runner eval_data/consumer_legal_retrieval --split $split --output "$WS/v4-retrieval-$split.json"
done
```

Expected: every command exits 0; `v4-notice-development.json` totals are 17 grounds, 1 labelled, 16 unlabelled, 0 known-bad.

- [ ] **Step 2: Dry-run one article and read the prompt**

Run: `.venv/Scripts/python.exe -m app.consumer.generate_aliases --dry-run --article br-cdc-art-39`
Expected: one request; its unit blocks start with `[br-cdc-art-39-…]` keys; inciso blocks include the caput lead-in; no golden text.

- [ ] **Step 3: STOP — confirm the real run with the user**

Ask: "Ready to generate aliases for 108 articles with `<LITIGATION_LLM_MODEL>` (OpenAI); only public statute text is sent. Proceed?" Wait for an explicit yes. Do not continue without it.

- [ ] **Step 4: Generate**

Run: `.venv/Scripts/python.exe -m app.consumer.generate_aliases`
Expected: exit 0 and a report; `written` close to 584. If it exits 1, rerun the same command (it resumes). Rerun once more for units listed under "no answer" or "fewer than two valid aliases"; units still listed after that stay without aliases and are recorded in the ledger.

- [ ] **Step 5: Inspect a sample**

```bash
.venv/Scripts/python.exe - <<'EOF'
import random
from app.consumer.aliases import load_alias_set
alias_set = load_alias_set()
print(len(alias_set.entries), "entries;", alias_set.model, alias_set.generated_on)
for entry in random.Random(0).sample(list(alias_set.entries), 8):
    print(entry.unit_key, "|", " / ".join(entry.aliases))
EOF
```

Expected: lay, unit-specific sentences. Record the entry count and the 8 samples in the ledger.

- [ ] **Step 6: Release v5 and move the dataset target**

In `app/consumer/legal_corpus.py` set `CONSUMER_LAW_CORPUS_RELEASE_ID = "br-consumer-law-<generated_on from Step 5>-v5"` and `LEGAL_CHUNKING_VERSION = "legal-hierarchy-v5"`. In `app/evaluation/label_ranks.py`, update the `DEFAULT_DEPTH` comment to name the new indexed chunk count (`len(get_default_legal_corpus().as_chunks())`) and confirm it is below 2,500. Then:

```bash
.venv/Scripts/python.exe - <<'EOF'
import json
from pathlib import Path
from app.consumer.legal_corpus import get_default_legal_corpus
corpus = get_default_legal_corpus()
path = Path("eval_data/consumer_legal_retrieval/dataset.json")
data = json.loads(path.read_text(encoding="utf-8"))
assert data["version"] == "2.2.0"
data["version"] = "2.3.0"
data["target_corpus_release_id"] = corpus.release_id
data["target_corpus_sha256"] = corpus.corpus_sha256
data["description"] += (
    " Version 2.3.0 targets corpus release v5 (lay-language alias chunks, ADR 0022);"
    " labels and splits are unchanged."
)
path.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
print(corpus.release_id, corpus.corpus_sha256, len(corpus.as_chunks()))
EOF
```

Update the version pins: in `tests/test_consumer_legal_corpus.py` `release_id.endswith("-v4")` → `"-v5"`; in `tests/test_consumer_evaluation.py` the two `"2.2.0"` assertions → `"2.3.0"` (rename `test_dataset_version_is_two_two_zero_with_an_explicit_holdout` to `test_dataset_version_keeps_the_explicit_holdout` and add to its docstring "2.3.0 only moves the target corpus to v5.").

- [ ] **Step 7: Measure v5 and compare**

```bash
for split in development holdout; do
  .venv/Scripts/python.exe -m app.evaluation.consumer_runner eval_data/consumer_legal_retrieval --evaluate-notice --split $split --output "$WS/v5-notice-$split.json"
  .venv/Scripts/python.exe -m app.evaluation.consumer_runner eval_data/consumer_legal_retrieval --split $split --output "$WS/v5-retrieval-$split.json"
  .venv/Scripts/python.exe -m app.evaluation.compare "$WS/v4-notice-$split.json" "$WS/v5-notice-$split.json" > "$WS/compare-notice-$split.txt"
  .venv/Scripts/python.exe -m app.evaluation.compare "$WS/v4-retrieval-$split.json" "$WS/v5-retrieval-$split.json" > "$WS/compare-retrieval-$split.txt"
done
cat "$WS/compare-notice-development.txt"
```

Expected: the compare reports show `changed corpus_release_id` and `dataset_sha256`.

- [ ] **Step 8: Apply the stop rules**

Stop and report the development comparison to the user — do not adjust pins or the prompt — if, on the development split, `consumer_notice_known_bad_citations` total is above 0, or the `consumer_notice_precision` average is below 0.042, or the `consumer_notice_article_recall` average is below 0.025. Otherwise continue.

- [ ] **Step 9: Re-baseline the pins and the CI gates**

Run the full suite (deselecting the demo test). For each failing test that pins a measurement of the default corpus, update the asserted value to the v5 measurement from `$WS/v5-*.json` (or the test's own output) and append a dated docstring or comment line: "Re-measured on <date> for corpus v5 (lay-language alias chunks, ADR 0022): <old> → <new>." Expected candidates:
- `test_offline_notice_baseline_on_the_seed_dataset` (totals, `by_split` averages);
- `test_an_agreement_sweep_retrieves_each_case_once` (shallow and default grounds);
- `test_verifier_removals_are_counted_per_case` (removed = default grounds);
- `test_cli_prints_an_agreement_sweep` (rows for depths 16 and 20);
- `tests/test_label_ranks.py::test_the_lexical_channel_shares_no_word_with_the_tie_in_sale_label` — if the alias now gives CDC art. 39 I a lexical rank, rename it to describe the new behaviour and assert the measured rank and verdict.

A failing test that is not a pinned measurement is a bug: stop and use superpowers:systematic-debugging.

In `.github/workflows/ci.yml`, set the development retrieval gates just below the v5 measurement (`recall@5`, `article_recall@5`, `ndcg@5` rounded down to two decimals; `hard_negative_rate@5` ceiling rounded up to two decimals) and `--min consumer_notice_labelled_grounds=<v5 development labelled total>`, keeping `--max consumer_notice_known_bad_citations=0`, `--min consumer_notice_abstention=1.0`, the inactive/unknown ceilings and success/abstention floors, each with a dated comment giving the measured values. Then run the two gated CI commands locally and confirm they exit 0.

- [ ] **Step 10: Verify and commit**

```bash
.venv/Scripts/python.exe -m pytest --cov --cov-report=term-missing -q -p no:cacheprovider --deselect tests/test_demo_manifest.py::test_demo_pdf_manifest_is_complete_current_and_reviewed
.venv/Scripts/python.exe -m ruff check app tests && .venv/Scripts/python.exe -m mypy app && .venv/Scripts/lint-imports && .venv/Scripts/vulture app --min-confidence 90
git add app/consumer/data/aliases/aliases.json app/consumer/legal_corpus.py app/evaluation/label_ranks.py eval_data/consumer_legal_retrieval/dataset.json .github/workflows/ci.yml tests
git commit -m "feat(consumer): corpus release v5 with generated lay-language aliases

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

---

### Task 6: ADR 0022 and the READMEs

**Files:**
- Create: `docs/adr/0022-lay-language-alias-chunks.md`
- Modify: `docs/architecture.md` (ADR index), `README.md`, `README-pt.md`

**Interfaces:**
- Consumes: `$WS/compare-notice-development.txt`, `$WS/compare-notice-holdout.txt`, `$WS/compare-retrieval-development.txt` and the Task 5 ledger lines (entry count, samples).

- [ ] **Step 1: Write ADR 0022**

Create `docs/adr/0022-lay-language-alias-chunks.md` with these sections, filling every number from the Task 5 outputs (no number may be left as a placeholder):

```markdown
# ADR 0022: Lay-language alias chunks

## Status

Accepted · Date: <commit date> · Amends: [ADR 0013](0013-versioned-consumer-law-retrieval.md),
[ADR 0019](0019-explicit-retrieval-agreement.md)

## Context

The retrieval-agreement gate cites a unit only when both the dense and the lexical
channel rank it within 13, and the lexical channel sets the ceiling: in dataset
2.1.0, 20 of 66 labels shared no token with their case's queries (ADR 0019). CDC
art. 39 I ("venda casada") shares none with its case; the water-outage case shares
one token with CDC art. 22. A fixed vocabulary appended to every query diluted
retrieval (ADR 0018); the missing words belong on the document side.

## Decision

1. Each active unit of a citable CDC, LGPD or Federal Constitution provision may
   carry 2–4 lay sentences in `app/consumer/data/aliases/aliases.json`, indexed as
   one alias chunk per unit (`…:alias-01`, `chunk_level: alias`). The Civil Code has
   none in this release.
2. An alias is never quoted: a citation found through one quotes the unit's official
   text and records the alias as `matched_chunk_id`; the trace marks it.
3. Aliases are generated offline by `python -m app.consumer.generate_aliases`, one
   LLM call per article, from the statute text alone (prompt
   `consumer-lay-aliases:v1`). The generator never reads the golden set.
4. Entries are `generated`, `reviewed` or `rejected`; the first two are indexed. An
   alias naming an article, statute or code is refused. An entry generated from text
   that has since changed fails corpus load.
5. Aliases are part of the corpus identity (corpus release v5,
   `legal-hierarchy-v5`); an empty alias set keeps earlier identities. Vector reuse
   leaves the existing chunks' vectors untouched; only alias chunks are embedded.

## Generation (<generated_on>)

<entry count> entries for <unit count> covered units from <model>; units left
without aliases: <count and why>. Eight sampled entries:

<the 8 samples from Task 5 Step 5, one per line>

## Measurements (offline stack, agreement depth 13)

| Development (24 cases) | v4 | v5 | 95% paired interval of the difference |
|---|---|---|---|
| notice article recall | | | |
| notice exact recall | | | |
| notice precision | | | |
| grounds / labelled / unlabelled / known-bad | | | |
| retrieval recall@5 / article recall@5 | | | |

| Holdout (19 cases, reported only) | v4 | v5 |
|---|---|---|
| notice article recall | | |
| notice precision | | |
| notice abstention | | |

<two or three sentences on what moved and why, citing cases from the compare output>

## Configured stack

Pending the protocol in the spec (§5.7): the golden query cache, a v4 baseline, the
v5 generation and a paired comparison. This section is completed when it runs.

## Consequences

- (+) Lay wording reaches units whose statutory text it never shares.
- (+) Citations, the gate and the eligibility policy are unchanged.
- (−) Up to one more same-article candidate per unit competes for the 8 slots per
  query (review step 4 addresses crowding).
- (−) Aliases are model output marked `requires_legal_review`.
```

Fill every table cell from the compare reports. Then append to `docs/architecture.md`'s ADR index:

```markdown
- [0022](adr/0022-lay-language-alias-chunks.md) — lay-language alias chunks
```

- [ ] **Step 2: Update the READMEs**

In `README.md`, section "Legal grounding and citations", after the bullet that begins "Statutory chunks preserve law, article, subdivision", add:

```markdown
- Each citable CDC, LGPD and Constitution unit may carry two to four lay sentences
  (`app/consumer/data/aliases/aliases.json`), indexed as a separate alias chunk so a
  complaint in everyday words can reach it. An alias is never quoted: a ground found
  through one cites the unit's official text and records the alias that matched
  (ADR 0022).
```

In the "Configuration" section, after the statute-snapshot refresh paragraph, add:

````markdown
Aliases are generated offline from the statute text alone, one LLM call per article,
with the configured LLM provider; the generator never reads the golden set:

```bash
python -m app.consumer.generate_aliases --dry-run --article br-cdc-art-39
python -m app.consumer.generate_aliases
```

The run resumes after an interruption and never replaces an entry marked `reviewed`
or `rejected` unless `--force` is given. Edit an entry's aliases by hand and set it to
`reviewed`, or set `rejected` to keep it out of the index. Corpus load refuses an entry
generated from statute text that has since changed.
````

In `README-pt.md`, add the Portuguese equivalents in the matching sections:

```markdown
- Cada unidade citável do CDC, da LGPD e da Constituição pode ter de duas a quatro
  frases leigas (`app/consumer/data/aliases/aliases.json`), indexadas como um chunk de
  alias separado, para que um relato em palavras comuns a alcance. Um alias nunca é
  citado: o fundamento encontrado por ele cita o texto oficial da unidade e registra o
  alias que o encontrou (ADR 0022).
```

````markdown
Os aliases são gerados offline só a partir do texto da lei, com uma chamada ao LLM
configurado por artigo; o gerador nunca lê o golden set:

```bash
python -m app.consumer.generate_aliases --dry-run --article br-cdc-art-39
python -m app.consumer.generate_aliases
```

A execução continua de onde parou após uma interrupção e nunca substitui uma entrada
marcada como `reviewed` ou `rejected`, a menos que se use `--force`. Edite os aliases
de uma entrada à mão e marque-a como `reviewed`, ou marque `rejected` para tirá-la do
índice. O carregamento do corpus recusa uma entrada gerada a partir de um texto legal
que mudou depois.
````

- [ ] **Step 3: Commit**

```bash
git add docs/adr/0022-lay-language-alias-chunks.md docs/architecture.md README.md README-pt.md
git commit -m "docs(adr-0022): lay-language alias chunks

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

---

### Task 7: The configured-stack protocol

**Files:**
- Modify: `docs/adr/0022-lay-language-alias-chunks.md` ("Configured stack" section)

**Interfaces:**
- Consumes: `python -m app.evaluation.query_vectors`, `consumer_runner --require-cached-queries` (ADR 0021), `python -m app.consumer.preindex_legal`, `compare`.

Every JUÁ command below runs with `HF_HUB_OFFLINE=1 TRANSFORMERS_OFFLINE=1 OMP_NUM_THREADS=2`.

- [ ] **Step 1: STOP — confirm the long CPU run with the user**

Ask: "Filling the JUÁ golden query cache loads the 4B model once per case (39 cases, about 1–2 hours of CPU). Run it in the background now?" Wait for an explicit yes.

- [ ] **Step 2: Fill the golden query cache, one case per process**

```bash
export HF_HUB_OFFLINE=1 TRANSFORMERS_OFFLINE=1 OMP_NUM_THREADS=2
CASES=$(.venv/Scripts/python.exe -c "from pathlib import Path; from app.evaluation.consumer_golden import load_consumer_legal_dataset; from app.evaluation.query_vectors import golden_queries; print(' '.join(golden_queries(load_consumer_legal_dataset(Path('eval_data/consumer_legal_retrieval')))))")
for case in $CASES; do .venv/Scripts/python.exe -m app.evaluation.query_vectors --case "$case" || echo "FAILED $case"; done > "$WS/fill-cache.log" 2>&1
.venv/Scripts/python.exe -m app.evaluation.query_vectors --check
```

Run the loop in the background and wait for its completion notice. Expected: `--check` ends with `coverage: 78/78` and exits 0. If any case failed, rerun the loop for the `FAILED` cases only (the cache resumes) until coverage is complete.

- [ ] **Step 3: Measure the v4 baseline from `main`**

```bash
git switch main
for split in development holdout; do
  .venv/Scripts/python.exe -m app.evaluation.consumer_runner --evaluate-notice --notice-pipeline configured --split $split --require-cached-queries --output "$WS/v4-configured-notice-$split.json"
  .venv/Scripts/python.exe -m app.evaluation.consumer_runner --retriever app.evaluation.consumer_retrievers:configured_hybrid_retriever --split $split --require-cached-queries --output "$WS/v4-configured-retrieval-$split.json"
done
git switch feat/lay-aliases
```

Expected: every run exits 0 without loading the model; no case is degraded (`consumer_notice_semantic_success` 1.0).

- [ ] **Step 4: STOP — confirm the v5 index build with the user**

Ask: "Building the v5 JUÁ generation embeds about 584 alias chunks (an hour or more of CPU), writes a new generation under `data/embedding_generations/` and a new Postgres namespace; the v4 ones are kept. Proceed?" Wait for an explicit yes.

- [ ] **Step 5: Build the v5 generation**

Run in the background: `.venv/Scripts/python.exe -m app.consumer.preindex_legal` (with the JUÁ environment). If it stops before completion, rerun it — completed shards are kept. Then `.venv/Scripts/python.exe -m app.consumer.preindex_legal --check`.
Expected: the reuse canary passes, only alias chunks are embedded, and `--check` reports the v5 corpus ready. If the canary fails, stop and report (do not pass `--no-reuse` without the user).

- [ ] **Step 6: Measure v5 and compare**

```bash
for split in development holdout; do
  .venv/Scripts/python.exe -m app.evaluation.consumer_runner --evaluate-notice --notice-pipeline configured --split $split --require-cached-queries --output "$WS/v5-configured-notice-$split.json"
  .venv/Scripts/python.exe -m app.evaluation.consumer_runner --retriever app.evaluation.consumer_retrievers:configured_hybrid_retriever --split $split --require-cached-queries --output "$WS/v5-configured-retrieval-$split.json"
  .venv/Scripts/python.exe -m app.evaluation.compare "$WS/v4-configured-notice-$split.json" "$WS/v5-configured-notice-$split.json" > "$WS/compare-configured-notice-$split.txt"
done
cat "$WS/compare-configured-notice-development.txt"
```

- [ ] **Step 7: Record the result and commit**

Replace the "Configured stack" section of ADR 0022 with the measured tables (same rows as the offline section, development with paired intervals, holdout reported) and two or three sentences of interpretation. Apply the same stop rules as Task 5 Step 8 to the configured development numbers and report them to the user either way.

```bash
git add docs/adr/0022-lay-language-alias-chunks.md
git commit -m "docs(adr-0022): configured-stack comparison of corpus v4 and v5

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```
