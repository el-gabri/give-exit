"""Notice-path evaluation: final grounds scored against the golden labels."""

from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path
from typing import Any

import pytest

from app.consumer.legal_corpus import get_default_legal_corpus
from app.consumer.legal_policy import LEGAL_GROUND_POLICY_VERSION
from app.consumer.schemas import LegalGround
from app.evaluation import consumer_runner
from app.evaluation.consumer_golden import load_consumer_legal_dataset
from app.evaluation.consumer_notice import (
    ConsumerNoticeGroundEvaluator,
    is_known_bad_citation,
    notice_ground_metrics,
    run_notice_evaluation,
)
from app.rag.embeddings import MockEmbeddingClient
from app.rag.pipeline import RagPipeline
from app.rag.vector_store import InMemoryVectorStore
from app.schemas.evaluation import ConsumerLegalGoldenCase, ConsumerLegalRelevance
from app.schemas.rag import RetrievedChunk

DATASET_PATH = Path("eval_data/consumer_legal_retrieval")


def _ground(unit_id: str) -> LegalGround:
    corpus = get_default_legal_corpus()
    chunk = next(
        item for item in corpus.as_chunks() if item.metadata.get("unit_id") == unit_id
    )
    return LegalGround(
        authority=corpus.authority_for_chunk(
            RetrievedChunk(chunk=chunk, score=0.03), retrieval_rank=1
        ),
        application_to_facts="fixture",
    )


def _case(**overrides: Any) -> ConsumerLegalGoldenCase:
    payload: dict[str, Any] = {
        "case_id": "notice_fixture",
        "category": "unauthorized_charge",
        "slices": ("supplier:telecom",),
        "complaint": "A operadora cobrou um pacote que eu nunca contratei e já paguei.",
        "desired_resolution": "Quero meu dinheiro de volta.",
        "relevant": (
            ConsumerLegalRelevance(
                article_id="br-cdc-art-42",
                unit_id="br-cdc-art-42-paragrafo-unico",
                grade=3,
                rationale="repetição do indébito",
            ),
            ConsumerLegalRelevance(
                article_id="br-cdc-art-6",
                unit_id="br-cdc-art-6-inciso-iii",
                grade=1,
                rationale="informação clara",
            ),
        ),
        "hard_negatives": ("br-cdc-art-43",),
    }
    payload.update(overrides)
    return ConsumerLegalGoldenCase(**payload)


class _BrokenQueryEmbedder(MockEmbeddingClient):
    async def embed_queries(self, texts: list[str]) -> list[list[float]]:
        raise RuntimeError("synthetic outage")


class _ExplodingPipeline:
    def retrieval_configuration(self, **_: object) -> dict[str, object]:
        return {"requested_k": 8, "retrieval_mode": "hybrid"}

    async def retrieve_many_with_traces(self, *_: object, **__: object) -> object:
        raise RuntimeError("store offline")


def test_known_bad_matching_is_exact_and_unit_aware() -> None:
    negatives = frozenset({"br-cdc-art-42", "br-cdc-art-18-paragrafo-1"})

    assert is_known_bad_citation("br-cdc-art-42", "br-cdc-art-42-paragrafo-unico", negatives)
    assert is_known_bad_citation(
        "br-cdc-art-18", "br-cdc-art-18-paragrafo-1-inciso-ii", negatives
    )
    assert not is_known_bad_citation("br-cdc-art-42-a", "br-cdc-art-42-a-caput", negatives)
    assert not is_known_bad_citation("br-cdc-art-18", "br-cdc-art-18-paragrafo-10", negatives)
    assert not is_known_bad_citation("br-cdc-art-18", None, negatives)


def test_final_grounds_are_scored_by_exact_unit_and_known_bad_citations() -> None:
    grounds = [
        _ground("br-cdc-art-42-paragrafo-unico"),
        _ground("br-cdc-art-43-paragrafo-2"),
    ]

    metrics, counts = notice_ground_metrics(grounds, _case(), degraded=False)

    assert counts == {
        "consumer_notice_grounds": 2,
        "consumer_notice_known_bad_citations": 1,
        "consumer_notice_complementary_grounds": 0,
    }
    assert {metric.name: metric.score for metric in metrics} == {
        "consumer_notice_semantic_success": 1.0,
        "consumer_notice_exact_recall": 0.5,
    }


def test_no_ground_cases_score_abstention_and_degradation() -> None:
    case = _case(
        category="no_consumer_relationship",
        relevant=(),
        hard_negatives=(),
        no_applicable_ground=True,
    )

    abstained, _ = notice_ground_metrics([], case, degraded=True)
    cited, _ = notice_ground_metrics(
        [_ground("br-cdc-art-42-paragrafo-unico")], case, degraded=False
    )

    assert {metric.name: metric.score for metric in abstained} == {
        "consumer_notice_semantic_success": 0.0,
        "consumer_notice_abstention": 1.0,
    }
    assert {metric.name: metric.score for metric in cited}["consumer_notice_abstention"] == 0.0


async def test_offline_notice_baseline_on_the_seed_dataset() -> None:
    """Re-measured on 2026-09-13 with the LGPD and the Civil Code in the corpus.

    On the CF+CDC corpus and the 15-case seed it was 79 grounds, 4 known-bad
    citations and exact recall 0.333 (measured 2026-09-07). Before Civil Code
    Título VI stopped being citable it was 47 grounds, 10 complementary.
    Change it deliberately.

    Re-measured on 2026-09-14 for the narrative-only builder (the fixed legal
    lexicon appended to every query was measured to dilute the case-specific
    signal and was removed; see app/consumer/retrieval.py). Dropping the
    lexicon query also drops the extra chunks it fed into every notice: total
    grounds fell from 70 to 23 and known-bad citations from 3 to 0 on this
    offline (hashed-embedder, effectively BM25) stack. On the configured JUÁ
    stack the one case this change targets improved similarly: Civil Code
    arts. 878, 880 and 881 (the undue-payment chapter, premised on a payment
    that never happened) stopped being cited and CDC art. 52 started being
    cited (3 grounds instead of 5, same relevant hit).

    Re-measured on 2026-09-24 for the explicit channel-agreement gate
    (consumer-notice-scope-eligibility-v4) and the index without uncitable
    chapters (legal-hierarchy-v4): 20 grounds instead of 23, 3 complementary
    instead of 5, still no known-bad citation and the same exact hit.

    Re-measured on 2026-09-24 for LGPD grounds without a CDC anchor
    (consumer-notice-scope-eligibility-v5, ADR 0020): the two data-protection
    cases that cited nothing now cite LGPD articles (arts. 5, 18 VII and 47;
    art. 50 § 2), so 24 grounds and 7 complementary. Still no known-bad
    citation and the same exact hit.

    Re-measured on 2026-09-24 for AGREEMENT_MAX_RANK 13 (ADR 0019), chosen on
    the configured JUÁ stack, where exact recall was 0.225 at every depth from
    12 to 24. Offline: 17 grounds, 5 complementary, no known-bad citation, and
    the single exact hit (CDC art. 12 for the allergen case) is lost, as it is
    at every depth below 20 on this hashed-embedder stack.

    Re-measured on 2026-09-24 for dataset 2.1.0 (15 new cases, the original 22
    unchanged): 22 grounds, 7 complementary, still no known-bad citation. The
    offline stack cites nothing in 9 of the 13 new in-scope cases, whose lay
    wording shares few words with the statute (see app.evaluation.label_ranks).
    """

    summary = await run_notice_evaluation(load_consumer_legal_dataset(DATASET_PATH))

    assert summary.failed_case_count == 0
    assert summary.totals == {
        "consumer_notice_complementary_grounds": 7,
        "consumer_notice_grounds": 22,
        "consumer_notice_known_bad_citations": 0,
    }
    assert summary.averages["consumer_notice_exact_recall"] == 0.0
    assert summary.averages["consumer_notice_abstention"] == 1.0
    assert summary.averages["consumer_notice_semantic_success"] == 1.0
    assert summary.run is not None
    assert summary.run.cutoffs == (8,)
    assert summary.run.ground_policy_version == LEGAL_GROUND_POLICY_VERSION
    assert summary.run.retrieval.retriever_id == "offline_notice_path"


async def test_degraded_retrieval_is_reported_per_case() -> None:
    corpus = get_default_legal_corpus()
    store = InMemoryVectorStore()
    await RagPipeline(MockEmbeddingClient(), store).index_chunks(corpus.as_chunks())
    evaluator = ConsumerNoticeGroundEvaluator(
        RagPipeline(_BrokenQueryEmbedder(), store), corpus, retriever_id="degraded_test"
    )
    dataset = load_consumer_legal_dataset(DATASET_PATH)
    single = dataset.model_copy(update={"cases": dataset.cases[:1]})

    summary = await evaluator.run(single)

    assert summary.cases[0].retrieval_outcome == "degraded"
    assert summary.averages["consumer_notice_semantic_success"] == 0.0


async def test_a_failing_case_is_recorded_without_stopping_the_run() -> None:
    corpus = get_default_legal_corpus()
    evaluator = ConsumerNoticeGroundEvaluator(
        _ExplodingPipeline(),  # type: ignore[arg-type]
        corpus,
        retriever_id="exploding",
    )

    summary = await evaluator.run(load_consumer_legal_dataset(DATASET_PATH))

    # Every case the scope gate lets through fails: 37 cases in dataset 2.1.0
    # minus the four no_applicable_ground ones the gate stops first.
    failed = [case for case in summary.cases if case.retrieval_outcome == "failed"]
    assert len(failed) == 33
    assert summary.failed_case_count == 33
    assert all("store offline" in case.errors[0] for case in failed)


def test_cli_writes_notice_results_and_gates_on_totals(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    output = tmp_path / "notice.json"
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "consumer_runner",
            str(DATASET_PATH),
            "--evaluate-notice",
            "--output",
            str(output),
            "--max",
            "consumer_notice_known_bad_citations=-1",
        ],
    )

    with pytest.raises(SystemExit) as excinfo:
        asyncio.run(consumer_runner._cli())

    assert excinfo.value.code == 1
    payload = json.loads(output.read_text(encoding="utf-8"))
    # Re-baselined on 2026-09-14 for the narrative-only builder: the measured
    # known-bad total on the offline (hashed-embedder) stack fell from 3 to 0
    # (the lexicon query that fed extra, sometimes wrong, chunks into every
    # notice is gone). The gate below is deliberately impossible (-1) so it
    # still exercises the CLI's failure path. See
    # test_offline_notice_baseline_on_the_seed_dataset for the full
    # explanation.
    assert payload["totals"]["consumer_notice_known_bad_citations"] == 0


def test_cli_rejects_require_semantic_without_notice_mode(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(sys, "argv", ["consumer_runner", "--require-semantic"])

    with pytest.raises(SystemExit) as excinfo:
        asyncio.run(consumer_runner._cli())

    assert excinfo.value.code == 2


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


async def _offline_evaluator(**kwargs: Any) -> ConsumerNoticeGroundEvaluator:
    corpus = get_default_legal_corpus()
    pipeline = RagPipeline(MockEmbeddingClient(), InMemoryVectorStore())
    await pipeline.index_chunks(corpus.as_chunks())
    return ConsumerNoticeGroundEvaluator(
        pipeline, corpus, retriever_id="sweep_test", **kwargs
    )


async def test_an_agreement_sweep_retrieves_each_case_once(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app.evaluation import consumer_notice

    calls: list[str] = []
    retrieve = consumer_notice.retrieve_legal_candidates

    async def counting(pipeline: RagPipeline, facts: Any, **kwargs: Any) -> Any:
        calls.append(facts.complaint_summary)
        return await retrieve(pipeline, facts, **kwargs)

    monkeypatch.setattr(consumer_notice, "retrieve_legal_candidates", counting)
    evaluator = await _offline_evaluator()
    dataset = load_consumer_legal_dataset(DATASET_PATH)

    shallow = await evaluator.run(dataset, agreement_max_rank=8)
    default = await evaluator.run(dataset)

    # 37 cases, four of them stopped by the scope gate before retrieval.
    assert len(calls) == 33
    assert shallow.run is not None and shallow.run.agreement_max_rank == 8
    assert default.run is not None and default.run.agreement_max_rank == 13
    assert default.run.ground_verifier == "none"
    # A shallower gate can only drop grounds.
    assert shallow.totals["consumer_notice_grounds"] == 11
    assert default.totals["consumer_notice_grounds"] == 22


class _RejectEveryGround:
    async def verify(self, facts: Any, grounds: list[LegalGround]) -> Any:
        from app.consumer.ground_verifier import GroundVerificationResult
        from app.consumer.schemas import GroundVerificationSummary
        from app.core.config import GroundVerifierMode

        del facts
        return GroundVerificationResult(
            grounds=[],
            summary=GroundVerificationSummary(mode=GroundVerifierMode.LLM, removed=len(grounds)),
        )


async def test_verifier_removals_are_counted_per_case() -> None:
    evaluator = await _offline_evaluator(ground_verifier=_RejectEveryGround())

    summary = await evaluator.run(load_consumer_legal_dataset(DATASET_PATH))

    assert summary.totals["consumer_notice_grounds"] == 0
    assert summary.totals["consumer_notice_verifier_removed"] == 22
    assert summary.totals["consumer_notice_verifier_failures"] == 0
    assert summary.run is not None and summary.run.ground_verifier == "llm"


def test_cli_prints_an_agreement_sweep(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    output = tmp_path / "sweep.json"
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "consumer_runner",
            str(DATASET_PATH),
            "--evaluate-notice",
            "--agreement-max-rank",
            "20",
            "--agreement-max-rank",
            "16",
            "--output",
            str(output),
        ],
    )

    asyncio.run(consumer_runner._cli())

    # Log lines from loggers an earlier test configured may precede the table.
    printed = capsys.readouterr().out.splitlines()
    start = next(i for i, line in enumerate(printed) if line.startswith("agreement_max_rank"))
    table = printed[start : start + 3]
    assert table[0].split()[:4] == ["agreement_max_rank", "grounds", "complementary", "known_bad"]
    assert [line.split()[:2] for line in table[1:]] == [["16", "27"], ["20", "33"]]
    payload = json.loads(output.read_text(encoding="utf-8"))
    assert sorted(payload["agreement_sweep"]) == ["16", "20"]
    assert payload["agreement_sweep"]["20"]["run"]["agreement_max_rank"] == 20


@pytest.mark.parametrize(
    "arguments",
    [
        ["--agreement-max-rank", "16"],
        ["--ground-verifier", "llm"],
        ["--evaluate-notice", "--agreement-max-rank", "0"],
        [
            "--evaluate-notice",
            "--agreement-max-rank",
            "16",
            "--agreement-max-rank",
            "20",
            "--max",
            "consumer_notice_known_bad_citations=0",
        ],
    ],
)
def test_cli_rejects_notice_options_that_cannot_apply(
    arguments: list[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(sys, "argv", ["consumer_runner", *arguments])

    with pytest.raises(SystemExit) as excinfo:
        asyncio.run(consumer_runner._cli())

    assert excinfo.value.code == 2


def test_cli_builds_the_llm_ground_verifier_from_settings(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app.consumer.ground_verifier import LLMGroundVerifier

    # The CLI reads the developer's .env; the test must not depend on it.
    monkeypatch.setenv("LITIGATION_LLM_PROVIDER", "mock")

    assert consumer_runner._ground_verifier("none") is None
    assert isinstance(consumer_runner._ground_verifier("llm"), LLMGroundVerifier)
