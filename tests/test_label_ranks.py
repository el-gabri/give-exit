"""The label-rank diagnostic explains which channel keeps a label out of a notice."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from app.consumer.legal_corpus import get_default_legal_corpus
from app.consumer.legal_policy import AGREEMENT_MAX_RANK
from app.evaluation import label_ranks
from app.evaluation.consumer_golden import load_consumer_legal_dataset
from app.evaluation.consumer_retrievers import offline_pipeline, prepare_evaluation_pipeline
from app.evaluation.label_ranks import LabelRank, label_ranks_for_case, rank_labels
from app.schemas.evaluation import ConsumerLegalRelevance
from app.schemas.rag import Chunk, RetrievedChunk

DATASET_PATH = Path("eval_data/consumer_legal_retrieval")


def _hit(unit_id: str | None, provision_id: str, **channel_ranks: int) -> RetrievedChunk:
    chunk = Chunk(
        chunk_id=f"doc:legal:{unit_id or provision_id}",
        doc_id="doc",
        text="texto",
        page_start=1,
        page_end=1,
        metadata={"unit_id": unit_id, "provision_id": provision_id},
    )
    return RetrievedChunk(chunk=chunk, score=0.01, channel_ranks=channel_ranks)


def _label(article_id: str, unit_id: str | None = None) -> ConsumerLegalRelevance:
    return ConsumerLegalRelevance(article_id=article_id, unit_id=unit_id, grade=3, rationale="r")


def test_the_best_rank_of_each_channel_and_the_depth_that_would_support_it() -> None:
    label = _label("br-cdc-art-42", "br-cdc-art-42-paragrafo-unico")
    result_sets = [
        [_hit("br-cdc-art-42-paragrafo-unico", "br-cdc-art-42", dense=30, lexical=2)],
        [_hit("br-cdc-art-42-paragrafo-unico", "br-cdc-art-42", dense=9, lexical=12)],
        # The whole article does not cite the unit.
        [_hit(None, "br-cdc-art-42", dense=1, lexical=1)],
    ]

    [rank] = label_ranks_for_case("case", [label], result_sets)

    assert rank == LabelRank(
        case_id="case",
        label="br-cdc-art-42-paragrafo-unico",
        dense=9,
        lexical=2,
        agreement_depth=12,
    )
    assert rank.verdict == "within the gate"


def test_an_article_label_matches_any_chunk_of_the_provision() -> None:
    [rank] = label_ranks_for_case(
        "case", [_label("br-cdc-art-52")], [[_hit(None, "br-cdc-art-52", dense=4, lexical=3)]]
    )

    assert rank.agreement_depth == 4


@pytest.mark.parametrize(
    ("dense", "lexical", "verdict"),
    [
        (AGREEMENT_MAX_RANK + 5, 1, "dense too deep"),
        (1, AGREEMENT_MAX_RANK + 5, "lexical too deep"),
        (AGREEMENT_MAX_RANK + 5, AGREEMENT_MAX_RANK + 5, "both too deep"),
    ],
)
def test_the_verdict_names_the_channel_that_fails(dense: int, lexical: int, verdict: str) -> None:
    rank = LabelRank("case", "label", dense, lexical, max(dense, lexical))

    assert rank.verdict == verdict


def test_a_chunk_only_one_channel_ranks_can_never_clear_the_gate() -> None:
    [rank] = label_ranks_for_case(
        "case", [_label("br-cdc-art-39", "br-cdc-art-39-inciso-i")],
        [[_hit("br-cdc-art-39-inciso-i", "br-cdc-art-39", dense=1)]],
    )

    assert (rank.dense, rank.lexical, rank.agreement_depth) == (1, None, None)
    assert rank.verdict == "one channel never ranks it"


async def test_the_lexical_channel_shares_no_word_with_the_tie_in_sale_label() -> None:
    """Offline lexical ranks are exact: they are the BM25 PostgreSQL computes.

    The tie-in sale complaint never uses the statute's words for it ("condicionar o
    fornecimento"), so the lexical channel cannot rank CDC art. 39 I at any depth.
    """
    pipeline = await prepare_evaluation_pipeline(offline_pipeline, get_default_legal_corpus())
    ranks = await rank_labels(pipeline, load_consumer_legal_dataset(DATASET_PATH))

    by_label = {(rank.case_id, rank.label): rank for rank in ranks}
    tie_in = by_label[("venda_casada_seguro", "br-cdc-art-39-inciso-i")]
    assert tie_in.lexical is None
    assert tie_in.verdict == "one channel never ranks it"
    assert by_label[("cobranca_indevida_ja_paga", "br-cdc-art-42-paragrafo-unico")].lexical == 2


async def test_cli_prints_the_table_and_writes_json(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    output = tmp_path / "ranks.json"

    await label_ranks._cli([str(DATASET_PATH), "--output", str(output)])

    printed = capsys.readouterr().out
    assert f"gate depth: {AGREEMENT_MAX_RANK}" in printed
    payload = json.loads(output.read_text(encoding="utf-8"))
    assert payload["pipeline"] == "offline"
    assert payload["gate_depth"] == AGREEMENT_MAX_RANK
    assert {"case_id", "label", "dense", "lexical", "agreement_depth", "verdict"} <= set(
        payload["labels"][0]
    )
