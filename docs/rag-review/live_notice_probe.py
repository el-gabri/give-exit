"""One production-shaped legal retrieval against the active generation."""

import asyncio
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from app.consumer.legal_corpus import get_default_legal_corpus
from app.consumer.legal_index import legal_corpus_is_indexed
from app.consumer.retrieval import build_legal_queries
from app.consumer.schemas import ConsumerCaseFacts
from app.consumer.service import ConsumerCaseService
from app.core.config import Settings
from app.core.logging import configure_logging
from app.evaluation.consumer_golden import load_consumer_legal_dataset
from app.rag.factory import create_rag_pipeline


async def main():
    configure_logging(level="WARNING")
    corpus = get_default_legal_corpus()
    rag = create_rag_pipeline(
        Settings(), corpus_version=f"{corpus.release_id}-{corpus.corpus_sha256[:12]}")
    if not await legal_corpus_is_indexed(rag, corpus):
        raise RuntimeError("active generation not ready; refusing to build it")
    dataset = load_consumer_legal_dataset(Path("eval_data/consumer_legal_retrieval"))
    case = next(c for c in dataset.cases if c.case_id == "cobranca_indevida_ja_paga")
    facts = ConsumerCaseFacts(complaint_summary=case.complaint,
                              desired_resolution=case.desired_resolution)
    results, traces = await rag.retrieve_many_with_traces(
        build_legal_queries(facts), doc_id=corpus.as_parsed_document().doc_id,
        agent="consumer_legal_authorities", k=8)
    service = object.__new__(ConsumerCaseService)
    service._legal_corpus = corpus
    grounds = service._legal_grounds(results, facts, traces)
    payload = {"case": case.case_id, "facts": facts.model_dump(mode="json"),
               "grounds": [g.authority.model_dump(mode="json") for g in grounds],
               "traces": [t.model_dump(mode="json") for t in traces]}
    Path("docs/rag-review/live-notice.json").write_text(
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({"grounds": [g.authority.unit_id or g.authority.provision_id for g in grounds],
                      "embedding_duration_ms": traces[0].embedding_duration_ms,
                      "batch_duration_ms": traces[0].batch_duration_ms,
                      "degraded_modes": [t.degraded_mode for t in traces]}, indent=2))


if __name__ == "__main__":
    asyncio.run(main())
