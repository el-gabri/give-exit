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
        silent: bool = False,
    ) -> None:
        self.calls: list[str] = []
        self.options: list[dict[str, Any]] = []
        self._overrides = overrides or {}
        self._extra = extra or []
        self._fail_on = fail_on
        self._silent = silent
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
            for key in ([] if self._silent else _KEY.findall(user))
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
    llm: _FakeLLM,
    alias_set: AliasSet,
    *articles: str,
    force: bool = False,
    today: date = TODAY,
) -> tuple[AliasSet, Any, list[AliasSet]]:
    saved: list[AliasSet] = []
    result, report = await generate_aliases(
        _corpus(),
        alias_set,
        llm,  # type: ignore[arg-type]
        model="fake-model",
        today=today,
        save=saved.append,
        articles=articles,
        force=force,
    )
    return result, report, saved


def test_one_request_per_covered_article_with_lead_ins() -> None:
    requests = alias_requests(_corpus())
    [article_39] = alias_requests(_corpus(), articles=["br-cdc-art-39"])
    provision = _corpus().get("br-cdc-art-39")

    # 108 articles less LGPD art. 43, whose four units are all controller
    # defenses ("só não serão responsabilizados quando") and are never sent.
    assert len(requests) == 107
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
    assert report.written and not report.missing and not report.declined
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


async def test_invalid_aliases_are_dropped_and_a_short_unit_declined() -> None:
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
    assert "br-cdc-art-39-inciso-ii" in report.declined
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
    # Staleness is per entry: the manifest may already name the current prompt
    # (an interrupted run) while some entries still come from an older one.
    older = first.model_copy(
        update={
            "entries": tuple(
                entry.model_copy(
                    update={"status": "reviewed", "prompt_version": "consumer-lay-aliases:v0"}
                )
                if entry.unit_key == key
                else entry.model_copy(update={"prompt_version": "consumer-lay-aliases:v0"})
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


async def test_new_entries_record_the_current_prompt_version() -> None:
    result, _, _ = await _run(_FakeLLM(), _empty(), "br-cf-art-5-xxxii")

    assert {entry.prompt_version for entry in result.entries} == {ALIAS_PROMPT_VERSION}


async def test_a_stale_entry_without_a_valid_replacement_is_declined_or_dropped() -> None:
    first, _, _ = await _run(_FakeLLM(), _empty(), "br-cdc-art-39")
    short_key, missing_key, reviewed_key = (
        "br-cdc-art-39-inciso-i",
        "br-cdc-art-39-inciso-ii",
        "br-cdc-art-39-inciso-iii",
    )
    stale = first.model_copy(
        update={
            "entries": tuple(
                entry.model_copy(
                    update={
                        "prompt_version": None,
                        "status": "reviewed" if entry.unit_key == reviewed_key else "generated",
                    }
                )
                for entry in first.entries
            )
        }
    )
    llm = _FakeLLM(overrides={short_key: [], reviewed_key: []})

    async def omit_missing(**kwargs: Any) -> ParsedResult[Any]:
        result = await _FakeLLM.parse(llm, **kwargs)
        units = [unit for unit in result.data.units if unit.unit_key != missing_key]
        return ParsedResult(data=result.data.model_copy(update={"units": units}), meta=result.meta)

    llm.parse = omit_missing  # type: ignore[method-assign]
    result, report, _ = await _run(llm, stale, "br-cdc-art-39", force=True)

    entries = {entry.unit_key: entry for entry in result.entries}
    assert entries[short_key].status == "declined"
    assert missing_key not in entries
    assert short_key in report.declined and missing_key in report.missing
    # A human-owned entry survives a forced run that brought nothing better.
    assert entries[reviewed_key].status == "reviewed"


def test_defense_and_exclusion_units_are_never_sent_to_the_model() -> None:
    [article_14] = alias_requests(_corpus(), articles=["br-cdc-art-14"])
    [article_7] = alias_requests(_corpus(), articles=["br-lgpd-art-7"])

    assert "br-cdc-art-14-caput" in article_14.sources
    assert not any(key.startswith("br-cdc-art-14-paragrafo-3") for key in article_14.sources)
    assert "br-cdc-art-14-paragrafo-2" not in article_14.sources
    assert "br-lgpd-art-7-inciso-i" in article_7.sources
    assert "br-lgpd-art-7-inciso-ii" not in article_7.sources
    assert "br-lgpd-art-7-inciso-x" not in article_7.sources


async def test_a_short_answer_is_recorded_as_declined_and_not_asked_again() -> None:
    key = "br-cdc-art-39-inciso-ii"
    first, report, _ = await _run(_FakeLLM(overrides={key: []}), _empty(), "br-cdc-art-39")
    rerun = _FakeLLM()
    second, _, saved = await _run(rerun, first, "br-cdc-art-39")

    declined = next(entry for entry in first.entries if entry.unit_key == key)
    assert (declined.status, declined.aliases) == ("declined", ())
    assert declined.prompt_version == ALIAS_PROMPT_VERSION
    assert report.declined == [key]
    assert rerun.calls == []
    assert saved == []
    assert second == first
    # The corpus accepts the entry and indexes no alias chunk for it.
    corpus = LegalCorpus(_corpus().provisions, aliases=first)
    assert not any(chunk.chunk_id.endswith(f":{key}:alias-01") for chunk in corpus.as_chunks())


async def test_an_empty_answer_keeps_every_entry_and_fails_the_article() -> None:
    first, _, _ = await _run(_FakeLLM(), _empty(), "br-cdc-art-39")
    stale = first.model_copy(
        update={
            "entries": tuple(
                entry.model_copy(update={"prompt_version": None}) for entry in first.entries
            )
        }
    )

    # A provider that answers with no requested unit (the mock client does)
    # must not wipe the entries it was asked to replace.
    result, report, saved = await _run(_FakeLLM(silent=True), stale, "br-cdc-art-39")

    assert report.failed == ["br-cdc-art-39"]
    assert result == stale
    assert saved == []
    assert "no requested unit answered: br-cdc-art-39" in render_report(report)


async def test_a_run_that_changes_no_entry_saves_nothing_and_keeps_the_manifest() -> None:
    key = "br-cf-art-5-xxxii"
    aliases = [
        "Quero que o Estado proteja quem compra produtos e serviços.",
        "Como consumidor, espero que o governo defenda meus interesses.",
    ]
    first, _, _ = await _run(_FakeLLM(overrides={key: aliases}), _empty(), key)

    again, _, saved = await _run(
        _FakeLLM(overrides={key: aliases}), first, key, force=True, today=date(2026, 10, 4)
    )

    assert saved == []
    assert again == first
    assert again.generated_on == TODAY


async def test_generated_entries_follow_the_units_the_generator_would_request() -> None:
    first, _, _ = await _run(_FakeLLM(), _empty(), "br-cdc-art-39")
    template = first.entries[0]

    def stray(unit_key: str, provision_id: str, status: str) -> Any:
        return template.model_copy(
            update={"unit_key": unit_key, "provision_id": provision_id, "status": status}
        )

    gone, gone_reviewed = "br-cdc-art-39-inciso-xcix", "br-cdc-art-39-inciso-xcviii"
    defense, defense_reviewed, defense_rejected = (
        "br-cdc-art-14-paragrafo-3-inciso-ii",
        "br-cdc-art-12-paragrafo-3",
        "br-cdc-art-14-paragrafo-2",
    )
    edited = first.model_copy(
        update={
            "entries": (
                *first.entries,
                stray(gone, "br-cdc-art-39", "generated"),
                stray(gone_reviewed, "br-cdc-art-39", "reviewed"),
                stray(defense, "br-cdc-art-14", "declined"),
                stray(defense_reviewed, "br-cdc-art-12", "reviewed"),
                stray(defense_rejected, "br-cdc-art-14", "rejected"),
            )
        }
    )
    rerun = _FakeLLM()

    result, report, saved = await _run(rerun, edited, "br-cdc-art-39")

    keys = {entry.unit_key for entry in result.entries}
    assert rerun.calls == []
    assert len(saved) == 1
    assert sorted(report.removed) == sorted([gone, defense])
    assert keys == {entry.unit_key for entry in first.entries} | {
        gone_reviewed,
        defense_reviewed,
        defense_rejected,
    }
    flagged = dict(item.split(": ", 1) for item in report.needs_review)
    assert set(flagged) == {gone_reviewed, defense_reviewed}
    assert "no longer a unit the generator requests" in flagged[gone_reviewed]
    assert "set its status to rejected" in flagged[defense_reviewed]
    assert "needs a person" in render_report(report)


async def test_a_stale_reviewed_entry_is_reported_for_a_person() -> None:
    first, _, _ = await _run(_FakeLLM(), _empty(), "br-cdc-art-39")
    key = "br-cdc-art-39-inciso-i"
    edited = first.model_copy(
        update={
            "entries": tuple(
                entry.model_copy(update={"status": "reviewed", "source_sha256": "0" * 64})
                if entry.unit_key == key
                else entry
                for entry in first.entries
            )
        }
    )

    _, report, saved = await _run(_FakeLLM(), edited, "br-cdc-art-39")

    assert saved == []
    assert report.needs_review == [
        f"{key}: reviewed against statute text that has since changed; set its status to "
        "generated to regenerate it, or to rejected"
    ]


async def test_the_cli_refuses_the_mock_provider(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    output = tmp_path / "aliases.json"
    monkeypatch.setenv("LITIGATION_LLM_PROVIDER", "mock")

    code = await alias_generation.main(["--article", "br-cf-art-5-xxxii", "--output", str(output)])

    assert code == 2
    assert "mock provider" in capsys.readouterr().err
    assert not output.exists()


async def test_the_cli_reports_a_provider_without_credentials_as_a_usage_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    def missing_key(_settings: Any) -> Any:
        raise ValueError("LITIGATION_OPENAI_API_KEY is required")

    monkeypatch.setattr(alias_generation, "create_llm_client", missing_key)

    with pytest.raises(SystemExit) as excinfo:
        await alias_generation.main(
            ["--article", "br-cf-art-5-xxxii", "--output", str(tmp_path / "aliases.json")]
        )

    assert excinfo.value.code == 2
    assert "LITIGATION_OPENAI_API_KEY is required" in capsys.readouterr().err


async def test_the_cli_fails_when_an_article_got_no_answer(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    output = tmp_path / "aliases.json"

    code = await _cli(monkeypatch, _FakeLLM(silent=True), output, "--article", "br-cf-art-5-xxxii")

    captured = capsys.readouterr()
    assert code == 1
    assert "no requested unit answered: br-cf-art-5-xxxii" in captured.out
    assert "rerun to retry" in captured.err
    assert not output.exists()

