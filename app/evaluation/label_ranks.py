"""Where each labelled provision ranks in the dense and the lexical channel.

The notice evaluation says whether a labelled provision was cited; this
diagnostic says why not. For every golden case with labels it runs the
production ranking queries with a deep candidate list and reports, per label,
the best rank each channel gave a matching chunk and the agreement depth that
would have supported it: the smallest, over the queries, of the worse of the
chunk's two ranks. A unit label matches only that unit's chunks, because a
notice citing the whole article does not cite the unit.

Offline the lexical ranks are exactly the ones PostgreSQL computes (the same
BM25) and the dense ranks come from a hashed bag of words, so only the
configured stack shows the real dense channel:

    python -m app.evaluation.label_ranks --pipeline configured --output label-ranks.json
"""

from __future__ import annotations

import argparse
import asyncio
import json
from collections import Counter
from collections.abc import Sequence
from dataclasses import asdict, dataclass
from pathlib import Path

from app.consumer.legal_corpus import get_default_legal_corpus
from app.consumer.legal_policy import AGREEMENT_MAX_RANK
from app.consumer.retrieval import build_legal_queries, is_consumer_scope
from app.consumer.schemas import ConsumerCaseFacts
from app.evaluation.consumer_golden import (
    load_consumer_legal_dataset,
    validate_consumer_legal_labels,
)
from app.evaluation.consumer_retrievers import (
    configured_pipeline,
    offline_pipeline,
    prepare_evaluation_pipeline,
)
from app.rag.pipeline import RagPipeline
from app.schemas.evaluation import ConsumerLegalGoldenDataset, ConsumerLegalRelevance
from app.schemas.rag import DENSE_CHANNEL, LEXICAL_CHANNEL, RetrievedChunk

# Fused results returned per query. It must exceed the number of indexed legal
# chunks (1,644 in legal-hierarchy-v4): a shorter fused list drops chunks that
# only one channel ranked, and their ranks would read as missing.
DEFAULT_DEPTH = 2_500


@dataclass(frozen=True, slots=True)
class LabelRank:
    """One labelled provision's best position in each channel for its case."""

    case_id: str
    label: str
    dense: int | None
    lexical: int | None
    agreement_depth: int | None

    @property
    def verdict(self) -> str:
        """Whether the gate would support the label, and if not, which channel fails."""
        if self.agreement_depth is None:
            return "one channel never ranks it"
        if self.agreement_depth <= AGREEMENT_MAX_RANK:
            return "within the gate"
        if (self.dense or 0) > AGREEMENT_MAX_RANK >= (self.lexical or 0):
            return "dense too deep"
        if (self.lexical or 0) > AGREEMENT_MAX_RANK >= (self.dense or 0):
            return "lexical too deep"
        return "both too deep"


def label_ranks_for_case(
    case_id: str, labels: Sequence[ConsumerLegalRelevance], result_sets: list[list[RetrievedChunk]]
) -> list[LabelRank]:
    """Best channel ranks of each label across one case's query results."""
    return [_rank_label(case_id, label, result_sets) for label in labels]


def _rank_label(
    case_id: str, label: ConsumerLegalRelevance, result_sets: list[list[RetrievedChunk]]
) -> LabelRank:
    dense: list[int] = []
    lexical: list[int] = []
    agreement: list[int] = []
    for results in result_sets:
        for item in results:
            if not _matches(item, label):
                continue
            ranks = item.channel_ranks
            if DENSE_CHANNEL in ranks:
                dense.append(ranks[DENSE_CHANNEL])
            if LEXICAL_CHANNEL in ranks:
                lexical.append(ranks[LEXICAL_CHANNEL])
            if DENSE_CHANNEL in ranks and LEXICAL_CHANNEL in ranks:
                agreement.append(max(ranks[DENSE_CHANNEL], ranks[LEXICAL_CHANNEL]))
    return LabelRank(
        case_id=case_id,
        label=label.unit_id or label.article_id,
        dense=min(dense, default=None),
        lexical=min(lexical, default=None),
        agreement_depth=min(agreement, default=None),
    )


def _matches(item: RetrievedChunk, label: ConsumerLegalRelevance) -> bool:
    metadata = item.chunk.metadata
    if label.unit_id is not None:
        return metadata.get("unit_id") == label.unit_id
    return metadata.get("provision_id") == label.article_id


async def rank_labels(
    pipeline: RagPipeline, dataset: ConsumerLegalGoldenDataset, *, depth: int = DEFAULT_DEPTH
) -> list[LabelRank]:
    """Rank every label of every in-scope labelled case with the production queries."""
    corpus = validate_consumer_legal_labels(dataset, corpus=get_default_legal_corpus())
    ranks: list[LabelRank] = []
    for case in dataset.cases:
        if not case.relevant or not is_consumer_scope(complaint=case.complaint):
            continue
        facts = ConsumerCaseFacts(
            complaint_summary=case.complaint, desired_resolution=case.desired_resolution
        )
        result_sets, _ = await pipeline.retrieve_many_with_traces(
            build_legal_queries(facts),
            doc_id=corpus.document_id,
            agent="label_ranks",
            k=depth,
            mode="hybrid",
        )
        ranks.extend(label_ranks_for_case(case.case_id, case.relevant, result_sets))
    return ranks


def render_label_ranks(ranks: Sequence[LabelRank]) -> str:
    """A table per label, then how many labels each verdict holds."""
    lines = [f"{'case':42} {'label':38} {'dense':>6} {'lexical':>7} {'depth':>6}  verdict"]
    for rank in ranks:
        lines.append(
            f"{rank.case_id[:42]:42} {rank.label[:38]:38} {_cell(rank.dense):>6} "
            f"{_cell(rank.lexical):>7} {_cell(rank.agreement_depth):>6}  {rank.verdict}"
        )
    lines.append("")
    lines.extend(
        f"{count:>3}  {verdict}"
        for verdict, count in Counter(rank.verdict for rank in ranks).most_common()
    )
    lines.append(f"gate depth: {AGREEMENT_MAX_RANK}")
    return "\n".join(lines)


def _cell(value: int | None) -> str:
    return "-" if value is None else str(value)


async def _cli(argv: Sequence[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0] if __doc__ else None)
    parser.add_argument("dataset", nargs="?", default="eval_data/consumer_legal_retrieval")
    parser.add_argument("--pipeline", choices=("offline", "configured"), default="offline")
    parser.add_argument("--depth", type=int, default=DEFAULT_DEPTH)
    parser.add_argument("--output", help="optional JSON output path")
    args = parser.parse_args(argv)
    dataset = load_consumer_legal_dataset(Path(args.dataset))
    factory = offline_pipeline if args.pipeline == "offline" else configured_pipeline
    pipeline = await prepare_evaluation_pipeline(factory, get_default_legal_corpus())
    try:
        ranks = await rank_labels(pipeline, dataset, depth=args.depth)
    finally:
        pipeline.close()
    if args.output:
        payload = {
            "pipeline": args.pipeline,
            "gate_depth": AGREEMENT_MAX_RANK,
            "labels": [{**asdict(rank), "verdict": rank.verdict} for rank in ranks],
        }
        Path(args.output).write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
    print(render_label_ranks(ranks))


if __name__ == "__main__":
    asyncio.run(_cli())
