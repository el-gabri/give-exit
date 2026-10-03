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
        self.options: list[dict[str, Any]] = []
        self._overrides = overrides or {}
        self._extra = extra or []
        self._fail_on = fail_on
        self._counter = 0

    async def complete(self, **_: Any) -> Any:  # pragma: no cover - never called
        raise AssertionError("the generator must use parse")

    async def parse(
        self, *, user: str, schema: type[BaseModel], **options: Any
    ) -> ParsedResult[Any]:
        self.calls.append(user)
        self.options.append(options)
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
        return [
            f"Situação de consumo número {self._counter}-{index} que vivi." for index in (1, 2, 3)
        ]


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
    valid = [
        "Comprei um celular e me obrigaram a levar a capinha.",
        "Fui obrigado a comprar dois itens juntos.",
    ]
    llm = _FakeLLM(
        overrides={
            "br-cdc-art-39-inciso-i": [legal, *valid],
            "br-cdc-art-39-inciso-ii": [legal, valid[0]],
        },
        extra=[
            {"unit_key": "br-cdc-art-40-caput", "aliases": valid},
            {
                "unit_key": "br-cdc-art-39-inciso-i",
                "aliases": ["Outra resposta duplicada qualquer.", *valid],
            },
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
    monkeypatch.setattr(
        alias_generation, "create_llm_client", lambda _settings: pytest.fail("no LLM")
    )

    dry_run = ["--dry-run", "--article", "br-cdc-art-39", "--output", str(output)]
    assert await alias_generation.main(dry_run) == 0
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


async def test_a_reasoning_model_is_called_through_its_effort_not_a_temperature(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # gpt-5.6-terra rejects temperature 0; the Responses path takes an effort instead.
    llm = _FakeLLM()
    output = tmp_path / "aliases.json"

    code = await _cli(
        monkeypatch, llm, output, "--article", "br-cf-art-5-xxxii", "--reasoning-effort", "low"
    )
    plain = _FakeLLM()
    await _run(plain, _empty(), "br-cf-art-5-xxxii")

    assert code == 0
    assert llm.options[0]["reasoning_effort"] == "low"
    assert llm.options[0]["max_output_tokens"] == alias_generation.ALIAS_MAX_OUTPUT_TOKENS
    assert plain.options[0]["reasoning_effort"] is None
    assert plain.options[0]["temperature"] == 0.0


async def test_a_new_prompt_version_regenerates_only_generated_entries() -> None:
    first, _, _ = await _run(_FakeLLM(), _empty(), "br-cdc-art-39")
    key = "br-cdc-art-39-inciso-i"
    older = first.model_copy(
        update={
            "prompt_version": "consumer-lay-aliases:v0",
            "entries": tuple(
                entry.model_copy(update={"status": "reviewed"}) if entry.unit_key == key else entry
                for entry in first.entries
            ),
        }
    )

    llm = _FakeLLM()
    second, report, _ = await _run(llm, older, "br-cdc-art-39")

    assert len(llm.calls) == 1
    assert report.skipped == 1
    assert len(report.written) == len(first.entries) - 1
    kept = next(entry for entry in second.entries if entry.unit_key == key)
    assert kept == next(entry for entry in older.entries if entry.unit_key == key)
    assert second.prompt_version == ALIAS_PROMPT_VERSION


def test_the_prompt_asks_for_the_units_distinguishing_condition() -> None:
    prompt = alias_generation.SYSTEM_PROMPT.casefold()

    assert "distingue" in prompt
    assert "lista vazia" in prompt
    assert ALIAS_PROMPT_VERSION == "consumer-lay-aliases:v2"

